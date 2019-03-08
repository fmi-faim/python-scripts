import logging
import subprocess
import os
import datetime
import time
import re

from concurrent.futures import ThreadPoolExecutor
from collections import namedtuple

from faim_robocopy.utils import is_filetree_a_subset_of
from faim_robocopy.utils import delete_existing
from faim_robocopy.utils import count_files_in_subtree
from faim_robocopy.utils import count_identical_files


def _sanitize_destinations(destinations):
    '''
    '''
    # check number of dest
    if not isinstance(destinations, (tuple, list)):
        destinations = [destinations]

    # warn user if a destination doesnt exist...
    for dest in destinations:
        if dest == '':
            pass
        elif not os.path.isdir(dest):
            logging.getLogger(__name__).warning(
                'Destination %s does not exist!', dest)

    # ...and clean it up
    return [
        dest for dest in destinations if dest != '' and os.path.exists(dest)
    ]


def _report(source, destinations, skip_files, n_deleted):
    '''report the number of present and identical files in source and
    destination folders.

    '''
    logger = logging.getLogger(__name__)

    for folder in [
            source,
    ] + destinations:

        if folder == '':
            continue

        try:
            filecount = count_files_in_subtree(folder)

            if folder != source:
                identical = count_identical_files(source, folder, skip_files)
                logger.info('%d files (total) in %s, %d identical to source',
                            filecount, folder, identical)
            else:
                logger.info('%d files (total) in %s', filecount, folder)
                if n_deleted > 0:
                    logger.info('%d files were deleted from %s', n_deleted,
                                folder)

        except Exception as err:
            logger.error('Could not count files in %s. Error: %s', folder,
                         str(err))


class RobocopyTask:
    '''Watches a source folder and launches robocopy calls for new data.
    Provides a terminate functionality to abort running threads preliminarily.

    '''

    def __init__(self):
        '''
        '''
        self._running = False
        self.futures = {}
        self._update_rate_in_s = 5.
        self._time_at_last_change = datetime.datetime.now()

    def terminate(self):
        '''requests the task to terminate.

        '''
        if self.is_running():
            logging.getLogger(__name__).warning('Stopping robocopy task')

        # prevent queued jobs from starting after terminate was called.
        for future in self.futures.values():
            future.cancel()

        self._running = False

    def _update_changed(self):
        '''
        '''
        self._time_at_last_change = datetime.datetime.now()

    def _wait_has_expired(self, time_to_exit_in_s):
        '''
        '''
        time_since_update = (datetime.datetime.now() -
                             self._time_at_last_change).total_seconds()
        logging.getLogger(__name__).debug(
            'Time since last detected change: %1.1f s',
            float(time_since_update))
        return time_since_update >= time_to_exit_in_s

    def is_running(self):
        '''
        '''
        return self._running

    def __enter__(self):
        '''
        '''
        logging.getLogger(__name__).info('Starting robocopy task')
        self._running = True

    def __exit__(self, *args, **kwargs):
        '''
        '''
        self._running = False

    def run(self, *args, **kwargs):
        '''runs the robocopy task.

        '''
        with self:
            return self._run(*args, **kwargs)

    def _run(self, source, destinations, multithread, time_interval, wait_exit,
             delete_source, skip_files, notifier, **robocopy_kwargs):
        '''actual robocopy task function. Call the public method to ensure that the
        is_running() state is properly set on entering and exiting.

        '''
        # Log start
        logger = logging.getLogger(__name__)

        # sanitize destinations
        destinations = _sanitize_destinations(destinations)

        if not destinations:
            raise RuntimeError('Need at least one destination to copy to.')

        logging.getLogger(__name__).info('Source folder: %s', source)
        for counter, dest in enumerate(destinations):
            logging.getLogger(__name__).info('Destination folder %d: %s',
                                             counter + 1, dest)

        # Define the number of threads for copying
        max_workers = 2 if (multithread and len(destinations) >= 2) else 1
        n_deleted = 0

        with ThreadPoolExecutor(max_workers=max_workers) as thread_pool:

            self._update_changed()

            def _robocopy_callback(future):
                '''handles the logging of robocopy jobs and sends a mail in case of
                failure.

                '''
                if future.cancelled():
                    logger.debug('Robocopy job cancelled')
                elif future.done():
                    error = future.exception()
                    if error:
                        # NOTE unfortunately, we dont know which destination
                        # the failing job had but we can report the error.
                        if isinstance(error, RobocopyError):
                            logger.error('%s', error)
                        else:
                            logger.error('Robocopy failed with error %s',
                                         error)
                        notifier.failed(error)
                    else:
                        logger.debug('Robocopy job terminated successfully')

            # Make at least one robocopy call for each directory
            # even if we dont have anything to do yet.
            self.futures = {
                dest: thread_pool.submit(
                    robocopy_call,
                    source=source,
                    dest=dest,
                    skip_files=skip_files,
                    **robocopy_kwargs)
                for dest in destinations
            }
            for future in self.futures.values():
                future.add_done_callback(_robocopy_callback)

            # Monitor source and dest folders and start robocopy jobs
            # whenever a source and destination have different content.
            while self.is_running():

                # prevent an early stop when the robocopy job is running long.
                if any(future.running() for future in self.futures.values()):
                    self._update_changed()

                # Terminate if wait_exit is expired without any new
                # file to copy.
                if self._wait_has_expired(wait_exit * 60.):
                    logger.info('Stopping robocopy after %1.1f min of waiting',
                                wait_exit)
                    break

                # For all those futures that are finished, we check if
                # there are new files.
                for dest in destinations:

                    if not is_filetree_a_subset_of(source, dest, skip_files):
                        self._update_changed()

                        if self.futures[dest].done():
                            self.futures[dest] = thread_pool.submit(
                                robocopy_call,
                                source=source,
                                dest=dest,
                                skip_files=skip_files,
                                **robocopy_kwargs)
                            self.futures[dest].add_done_callback(
                                _robocopy_callback)

                # delete files that are copied to all destinations.
                if delete_source:
                    n_deleted += delete_existing(source, destinations)

                # wait
                logger.info(
                    'Waiting for %1.1f min before checking for next Robocopy',
                    float(time_interval))

                # Sleep with polling for a potential terminate() signal
                for _ in range(
                        int(time_interval * 60. / self._update_rate_in_s)):
                    time.sleep(self._update_rate_in_s)
                    if not self.is_running():
                        break

        # Report files in both folders.
        logger.info('Robocopy summary:')
        _report(source, destinations, skip_files, n_deleted)

        # Notify user about success.
        notifier.finished()


def robocopy_call(source, dest, silent, secure_mode, skip_files):
    '''run an individual robocopy call.

    Parameters
    ----------
    source : path
        source folder.
    dest : path
        destination folder.
    silent : bool
        silence robocopy output.
    secure_mode : bool
        run robocopy with secure mode flags.
    skip_files : string
        file ending to ignore.

    Notes
    -----
    An error is raised if Robocopy returns with an exit code >= 8.

    '''
    exclude_files = "*." + skip_files  # TODO Refactor

    # Robocopy syntax:
    # robocopy <Source> <Destination> [<File>[ ...]] [<Options>]
    # - /XF: exclude files
    # - /e:  copy subdirectories
    #
    # https://docs.microsoft.com/en-us/windows-server/administration/windows-commands/robocopy
    cmd = ["robocopy", source, dest, "/XF", exclude_files, "/e", "/COPY:DT"]

    if secure_mode == 1:
        cmd.append("/r:0")
        cmd.append("/w:30")
        cmd.append("/dcopy:T")
        cmd.append("/Z")

    # remove job header and summary from log, but be verbose about files.
    cmd.extend(['/V', '/njh', '/njs'])

    call_kwargs = dict()
    if silent == 0:
        call_kwargs['shell'] = True

    try:
        subprocess.check_output(cmd, **call_kwargs)
    except subprocess.CalledProcessError as err:
        exit_code = err.returncode
        logging.getLogger(__name__).debug('Robocopy nonzero exit code: %s',
                                          exit_code)

        # Return codes above 8 are errors
        if exit_code >= 8:
            raise RobocopyError.from_error(err) from err
        elif 2 <= exit_code < 8:
            logging.getLogger(__name__).debug(
                'Robocopy exited with code %d. This is not a failure.',
                exit_code)


class RobocopyError(Exception):
    '''Robocopy exception for return codes >= 8.

    '''
    def __init__(self, returncode, error_info):
        '''
        '''
        super().__init__()
        self.returncode = returncode
        self.error_info = error_info

    @classmethod
    def from_error(cls, called_subprocess_error):
        '''create a RobocopyError from a CalledProcessError.

        '''
        return cls(
            returncode=called_subprocess_error.returncode,
            error_info=parse_errors_from_robocopy_stdout(
                called_subprocess_error.output))

    def __str__(self):
        '''format error message.

        '''
        msg = 'Robocopy returned with exit code %s.' % self.returncode
        if not self.error_info or self.error_info is None:
            msg += ' No detailled information available.'
            return msg

        msg += ' The following issues were encountered:\n'
        for code, action, reason in self.error_info:
            msg += '  [Code %s] %s: %s\n' % (code, action, reason)
        return msg


def parse_errors_from_robocopy_stdout(output):
    '''parse error information from output of a single robocopy run.

    '''
    output = output.decode('UTF-8')
    logger = logging.getLogger(__name__ + '.parser')
    pattern = re.compile(r'ERROR\s+(\d+)\s+\(0x[0-9a-fA-F]+\)\s+(.*)\n^(.*)$',
                         re.MULTILINE)

    matches = re.findall(pattern, output)
    if not matches:
        logger.debug('Could not parse any errors from stdout of robocopy')
        logger.debug('Raw robocopy stdout:\n %s', output)
        return None

    RobocopyErrorInfo = namedtuple('RobocopyErrorInfo',
                                   ['code', 'action', 'reason'])

    return [
        RobocopyErrorInfo(*[val.strip('\r') for val in match])
        for match in matches
    ]
