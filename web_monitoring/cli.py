# Command Line Interface
# See scripts/ directory for associated executable(s). All of the interesting
# functionality is implemented in this module to make it easier to test.
from collections import defaultdict
from datetime import datetime, timedelta
from docopt import docopt
import json
import logging
from os.path import splitext
import pandas
from pathlib import Path
import re
import requests
import signal
import sys
import threading
from tqdm import tqdm
from urllib.parse import urlparse
from web_monitoring import db
from web_monitoring import internetarchive as ia
from web_monitoring import utils

import queue
import asyncio
import concurrent


logger = logging.getLogger(__name__)

PARALLEL_REQUESTS = 10

HOST_EXPRESSION = re.compile(r'^[^:]+://([^/]+)')
INDEX_PAGE_EXPRESSION = re.compile(r'index(\.\w+)?$')
SUBRESOURCE_MIME_TYPES = (
    'text/css',
    'text/javascript',
    'application/javascript',
    'image/jpeg',
    'image/webp',
    'image/png',
    'image/gif',
    'image/bmp',
    'image/tiff',
    'image/x-icon',
)
SUBRESOURCE_EXTENSIONS = (
    '.css',
    '.js',
    '.es',
    '.es6',
    '.jsm',
    '.jpg',
    '.jpeg',
    '.webp',
    '.png',
    '.gif',
    '.bmp',
    '.tif',
    '.ico',
)
# Never query CDX for *all* snapshots at any of these domains (instead, always
# query for each specific URL we want).
NEVER_QUERY_DOMAINS = (
    'instagram.com',
    'youtube.com',
    'amazon.com'
)
# Query an entire domain for snapshots if we are interested in more than this
# many URLs in the domain (NEVER_QUERY_DOMAINS above overrides this).
MAX_QUERY_URLS_PER_DOMAIN = 30


# These functions lump together library code into monolithic operations for the
# CLI. They also print. To access this functionality programmatically, it is
# better to use the underlying library code.

def _get_progress_meter(iterable):
    # Use TQDM in all environments, but don't update very often if not a TTY.
    # Basically, the idea here is to keep TQDM in our logs so we get stats, but
    # not to waste a huge amount of space in the logs with it.
    # NOTE: This is cribbed from TQDM's `disable=None` logic:
    # https://github.com/tqdm/tqdm/blob/f2a60d1fb9e8a15baf926b4a67c02f90e0033eba/tqdm/_tqdm.py#L817-L830
    file = sys.stderr
    intervals = {}
    if hasattr(file, "isatty") and not file.isatty():
        intervals = dict(mininterval=10, maxinterval=60)

    return tqdm(iterable, desc='importing', unit=' versions', **intervals)


def _add_and_monitor(versions, create_pages=True, skip_unchanged_versions=True, stop_event=None):
    cli = db.Client.from_env()  # will raise if env vars not set
    # Wrap verions in a progress bar.
    # TODO: create this on the main thread so we can update totals when we
    # discover them in CDX, but update progress here as we import.
    versions = _get_progress_meter(versions)
    import_ids = cli.add_versions(versions, create_pages=create_pages,
                                  skip_unchanged_versions=skip_unchanged_versions)
    print('Import jobs IDs: {}'.format(import_ids))
    print('Polling web-monitoring-db until import jobs are finished...')
    errors = cli.monitor_import_statuses(import_ids, stop_event)
    if errors:
        print("Errors: {}".format(errors))


def _log_adds(versions):
    versions = _get_progress_meter(versions)
    for version in versions:
        print(json.dumps(version))


class WaybackRecordsWorker(threading.Thread):
    def __init__(self, records, results_queue, maintainers, tags, cancel,
                 failure_queue=None, session_options=None,
                 unplaybackable=None):
        super().__init__()
        self.summary = self.create_summary()
        self.results_queue = results_queue
        self.failure_queue = failure_queue
        self.cancel = cancel
        self.records = records
        self.maintainers = maintainers
        self.tags = tags
        self.unplaybackable = unplaybackable
        self.session_options = session_options or dict(retries=3, backoff=2,
                                                       timeout=(30.5, 2))
        self.session = None
        self.wayback = None

    def reset_client(self):
        if self.session:
            self.session.close()
        self.session = ia.WaybackSession(**self.session_options)
        self.wayback = ia.WaybackClient(session=self.session)

    def is_active(self):
        return not self.cancel.is_set()

    def run(self):
        """
        Work through the queue of CDX records to load them from Wayback,
        transform them to Web Monitoring DB import entries, and queue them for
        importing.
        """
        self.reset_client()

        while self.is_active():
            try:
                record = next(self.records)
                self.summary['total'] += 1
            except StopIteration:
                break

            self.handle_record(record, retry_connection_failures=True)

        self.wayback.close()
        return self.summary

    def handle_record(self, record, retry_connection_failures=False):
        """
        Handle a single CDX record.
        """
        # Check for whether we already know this can't be played and bail out.
        if self.unplaybackable is not None and record.raw_url in self.unplaybackable:
            self.summary['playback'] += 1
            return

        try:
            version = self.process_record(record, retry_connection_failures=True)
            self.results_queue.put(version)
            self.summary['success'] += 1
        except ia.MementoPlaybackError as error:
            self.summary['playback'] += 1
            if self.unplaybackable is not None:
                self.unplaybackable[record.raw_url] = datetime.utcnow()
            logger.info(f'  {error}')
        except requests.exceptions.HTTPError as error:
            if error.response.status_code == 404:
                logger.info(f'  Missing memento: {record.raw_url}')
                self.summary['missing'] += 1
            else:
                # TODO: consider not logging this at a lower level, like debug
                # unless failure_queue does not exist. Unsure how big a deal
                # this error is to log if we are retrying.
                logger.info(f'  (HTTPError) {error}')
                # TODO: definitely don't count it if we are going to retry it
                self.summary['unknown'] += 1
                if self.failure_queue:
                    self.failure_queue.put(record)
        except ia.WaybackRetryError as error:
            # TODO: don't count or log (well, maybe DEBUG log) if failure_queue
            # is present and we are ultimately going to retry.
            self.summary['unknown'] += 1
            logger.info(f'  {error}; URL: {record.raw_url}')

            if self.failure_queue:
                self.failure_queue.put(record)
        except Exception as error:
            # TODO: don't count or log (well, maybe DEBUG log) if failure_queue
            # is present and we are ultimately going to retry.
            self.summary['unknown'] += 1
            logger.exception(f'  ({type(error)}) {error}; URL: {record.raw_url}')

            if self.failure_queue:
                self.failure_queue.put(record)

    def process_record(self, record, retry_connection_failures=False):
        """
        Load the actual Wayback memento for a CDX record and transform it to
        a Web Monitoring DB import record.
        """
        try:
            return self.wayback.timestamped_uri_to_version(record.date,
                                                           record.raw_url,
                                                           url=record.url,
                                                           maintainers=self.maintainers,
                                                           tags=self.tags,
                                                           view_url=record.view_url)
        except Exception as error:
            # On connection failures, reset the session and try again. If we
            # don't do this, the connection pool for this thread is pretty much
            # dead. It's not clear to me whether there is a problem in urllib3
            # or Wayback's servers that requires this.
            # This unfortunately requires string checking because the error can
            # get wrapped up into multiple kinds of higher-level errors :(
            if retry_connection_failures and ('failed to establish a new connection' in str(error).lower()):
                self.reset_client()
                return self.process_record(record)

            # Otherwise, re-raise the error.
            raise error

    @classmethod
    def create_summary(cls):
        return {'total': 0, 'success': 0, 'playback': 0, 'missing': 0,
                'unknown': 0}

    @classmethod
    def summarize(cls, workers):
        return cls.merge_summaries((w.summary for w in workers))

    @classmethod
    def merge_summaries(cls, summaries):
        merged = cls.create_summary()
        for summary in summaries:
            for key in merged.keys():
                merged[key] += summary[key]

        # Add percentage calculations
        if merged['total']:
            merged.update({f'{k}_pct': 100 * v / merged['total']
                           for k, v in merged.items()
                           if k != 'total' and not k.endswith('_pct')})
        else:
            merged.update({f'{k}_pct': 0.0
                           for k, v in merged.items()
                           if k != 'total' and not k.endswith('_pct')})

        return merged


def iterate_into_queue(iterable, queue):
    for item in iterable:
        queue.put(item)
    queue.put(None)


async def import_ia_db_urls(*, from_date=None, to_date=None, maintainers=None,
                            tags=None, skip_unchanged='resolved-response',
                            url_pattern=None, worker_count=0,
                            unplaybackable_path=None, dry_run=False):
    client = db.Client.from_env()
    logger.info('Loading known pages from web-monitoring-db instance...')
    urls, version_filter = _get_db_page_url_info(client, url_pattern)

    # Wayback search treats URLs as SURT, so dedupe obvious repeats first.
    www_subdomain = re.compile(r'^https?://www\d*\.')
    urls = set((www_subdomain.sub('http://', url) for url in urls))

    logger.info(f'Found {len(urls)} CDX-queryable URLs')
    logger.debug('\n  '.join(urls))

    return await import_ia_urls(
        urls=urls,
        from_date=from_date,
        to_date=to_date,
        maintainers=maintainers,
        tags=tags,
        skip_unchanged=skip_unchanged,
        version_filter=version_filter,
        worker_count=worker_count,
        create_pages=False,
        unplaybackable_path=unplaybackable_path,
        dry_run=dry_run)


def load_from_wayback(records, versions_queue, maintainers, tags, cancel, unplaybackable, worker_count, tries=None):
    if tries is None or len(tries) == 0:
        tries = (None,)
    if isinstance(records, queue.Queue):
        records = utils.ThreadSafeIterator(utils.queue_iterator(records))

    summary = WaybackRecordsWorker.create_summary()

    total_tries = len(tries)
    retry_queue = None
    for index, try_setting in enumerate(tries):
        if retry_queue and not retry_queue.empty():
            print(f'\nRetrying about {retry_queue.qsize()} failed records...', flush=True)
            retry_queue.put(None)
            records = utils.ThreadSafeIterator(utils.queue_iterator(retry_queue))

        if index == total_tries - 1:
            retry_queue = None
        else:
            retry_queue = queue.Queue()

        workers = []
        for i in range(worker_count):
            worker = WaybackRecordsWorker(records, versions_queue,
                                          maintainers, tags, cancel,
                                          retry_queue, try_setting,
                                          unplaybackable)
            workers.append(worker)
            worker.start()

        for worker in workers:
            worker.join()

        try_summary = WaybackRecordsWorker.summarize(workers)
        if index == 0:
            summary = try_summary
        else:
            summary['success'] += try_summary['success']
            summary['playback'] += try_summary['playback']
            summary['missing'] += try_summary['missing']
            summary['unknown'] -= (try_summary['success'] +
                                   try_summary['playback'] +
                                   try_summary['missing'])

    # Recalculate percentages
    summary = WaybackRecordsWorker.merge_summaries([summary])
    return summary


# TODO: this function probably be split apart so `dry_run` doesn't need to
# exist as an argument.
async def import_ia_urls(urls, *, from_date=None, to_date=None,
                         maintainers=None, tags=None,
                         skip_unchanged='resolved-response',
                         version_filter=None, worker_count=0,
                         create_pages=True, unplaybackable_path=None,
                         dry_run=False):
    skip_responses = skip_unchanged == 'response'
    worker_count = worker_count if worker_count > 0 else PARALLEL_REQUESTS
    unplaybackable = load_unplaybackable_mementos(unplaybackable_path)

    # Use a custom session to make sure CDX calls are extra robust.
    session = ia.WaybackSession(retries=10, backoff=4)

    with utils.QuitSignal((signal.SIGINT, signal.SIGTERM)) as stop_event:
        with ia.WaybackClient(session) as wayback:
            # wayback_records = utils.ThreadSafeIterator(
            #     _list_ia_versions_for_urls(
            #         urls,
            #         from_date,
            #         to_date,
            #         skip_responses,
            #         version_filter,
            #         client=wayback))

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count + 1)
            loop = asyncio.get_event_loop()
            versions_queue = queue.Queue()
            versions = utils.queue_iterator(versions_queue)
            if skip_unchanged == 'resolved-response':
                versions = _filter_unchanged_versions(versions)

            if dry_run:
                uploader = loop.run_in_executor(executor, _log_adds, versions)
            else:
                uploader = loop.run_in_executor(executor, _add_and_monitor, versions, create_pages, stop_event)

            # summary = merge_worker_summaries([worker_summary()])
            summary = WaybackRecordsWorker.create_summary()
            all_records = _list_ia_versions_for_urls(
                urls,
                from_date,
                to_date,
                skip_responses,
                version_filter,
                client=wayback,
                stop=stop_event)
            # for wayback_records in toolz.partition_all(2000, all_records):
            # wayback_records = utils.ThreadSafeIterator(all_records)
            wayback_records = queue.Queue()
            cdx_task = loop.run_in_executor(executor, iterate_into_queue, all_records, wayback_records)

            retry_settings = (None,
                              dict(retries=3, backoff=4, timeout=(30.5, 2)),
                              dict(retries=7, backoff=4, timeout=60.5))
            summary = load_from_wayback(wayback_records,
                                        versions_queue,
                                        maintainers,
                                        tags,
                                        stop_event,
                                        unplaybackable,
                                        worker_count,
                                        retry_settings)

            print('\nLoaded {total} CDX records:\n'
                  '  {success:6} successes ({success_pct:.2f}%),\n'
                  '  {playback:6} could not be played back ({playback_pct:.2f}%),\n'
                  '  {missing:6} had no actual memento ({missing_pct:.2f}%),\n'
                  '  {unknown:6} unknown errors ({unknown_pct:.2f}%).'.format(
                    **summary))

            # Signal that there will be nothing else on the queue so uploading can finish
            versions_queue.put(None)

            if not dry_run:
                print('Saving list of non-playbackable URLs...')
                save_unplaybackable_mementos(unplaybackable_path, unplaybackable)

            await cdx_task
            await uploader


def _filter_unchanged_versions(versions):
    """
    Take an iteratable of importable version dicts and yield only versions that
    differ from the previous version of the same page.
    """
    last_hashes = {}
    for version in versions:
        if last_hashes.get(version['page_url']) != version['version_hash']:
            last_hashes[version['page_url']] = version['version_hash']
            yield version


def _list_ia_versions_for_urls(url_patterns, from_date, to_date,
                               skip_repeats=True, version_filter=None,
                               client=None, stop=None):
    version_filter = version_filter or _is_page
    skipped = 0

    with client or ia.WaybackClient() as client:
        for url in url_patterns:
            if stop and stop.is_set():
                break

            ia_versions = client.list_versions(url,
                                            from_date=from_date,
                                            to_date=to_date,
                                            skip_repeats=skip_repeats)
            try:
                for version in ia_versions:
                    if stop and stop.is_set():
                        break
                    if version_filter(version):
                        yield version
                    else:
                        skipped += 1
                        logger.debug('Skipping URL "%s"', version.url)
            except ia.BlockedByRobotsError as error:
                logger.warn(str(error))
            except ValueError as error:
                # NOTE: this isn't really an exceptional case; list_versions()
                # raises ValueError when Wayback has no matching records.
                # TODO: there should probably be no exception in this case.
                if 'does not have archived versions' not in str(error):
                    logger.warn(error)
            except ia.WaybackException as error:
                logger.error(f'Error getting CDX data for {url}: {error}')
            except Exception:
                # Need to handle the exception here to let iteration continue
                # and allow other threads that might be running to be joined.
                logger.exception(f'Error processing versions of {url}')

    if skipped > 0:
        logger.info('Skipped %s URLs that did not match filters', skipped)


def load_unplaybackable_mementos(path):
    unplaybackable = {}
    if path:
        try:
            with open(path) as file:
                unplaybackable = json.load(file)
        except FileNotFoundError:
            pass
    return unplaybackable


def save_unplaybackable_mementos(path, mementos, expiration=7 * 24 * 60 * 60):
    if path is None:
        return

    threshold = datetime.utcnow() - timedelta(seconds=expiration)
    urls = list(mementos.keys())
    for url in urls:
        date = mementos[url]
        needs_format = False
        if isinstance(date, str):
            date = datetime.strptime(date, '%Y-%m-%dT%H:%M:%SZ')
        else:
            needs_format = True

        if date < threshold:
            del mementos[url]
        elif needs_format:
            mementos[url] = date.isoformat(timespec='seconds') + 'Z'

    file_path = Path(path)
    if not file_path.parent.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open('w') as file:
        json.dump(mementos, file)


# XXX: This is no longer really listing domains. Should we find a way to make
# it do that again, or simply remove this functionality?
def list_domains(url_pattern=None):
    client = db.Client.from_env()
    logger.info('Loading known pages from web-monitoring-db instance...')
    domains, version_filter = _get_db_page_url_info(client, url_pattern)

    text = '\n  '.join(domains)
    print(f'Found {len(domains)} matching domains:\n  {text}')


def _can_query_domain(domain):
    if domain in NEVER_QUERY_DOMAINS:
        return False

    return next((False for item in NEVER_QUERY_DOMAINS
                if domain.endswith(f'.{item}')), True)


def _get_db_page_url_info(client, url_pattern=None):
    # If these sets get too big, we can switch to a bloom filter. It's fine if
    # we have some false positives. Any noise reduction is worthwhile.
    url_keys = set()
    domains = defaultdict(lambda: {'query_domain': False, 'urls': []})

    domains_without_url_keys = set()
    for page in _list_all_db_pages(client, url_pattern):
        domain = HOST_EXPRESSION.match(page['url']).group(1)
        data = domains[domain]
        if not data['query_domain']:
            if len(data['urls']) >= MAX_QUERY_URLS_PER_DOMAIN and _can_query_domain(domain):
                data['query_domain'] = True
            else:
                data['urls'].append(page['url'])

        if domain in domains_without_url_keys:
            continue

        url_key = page['url_key']
        if url_key:
            url_keys.add(_rough_url_key(url_key))
        else:
            domains_without_url_keys.add(domain)
            logger.warn('Found DB page with no url_key; *all* pages in '
                        f'"{domain}" will be imported')

    def filterer(version, domain=None):
        domain = domain or HOST_EXPRESSION.match(version.url).group(1)
        if domain in domains_without_url_keys:
            return _is_page(version)
        else:
            return _rough_url_key(version.key) in url_keys

    url_list = []
    for domain, data in domains.items():
        if data['query_domain']:
            url_list.append(f'http://{domain}/*')
        else:
            url_list.extend(data['urls'])

    return url_list, filterer


def _rough_url_key(url_key):
    """
    Create an ultra-loose version of a SURT key that should match regardless of
    most SURT settings. (This allows lots of false positives.)
    """
    rough_key = url_key.lower()
    rough_key = rough_key.split('?', 1)[0]
    rough_key = rough_key.split('#', 1)[0]
    rough_key = INDEX_PAGE_EXPRESSION.sub('', rough_key)
    if rough_key.endswith('/'):
        rough_key = rough_key[:-1]
    return rough_key


def _is_page(version):
    """
    Determine if a version might be a page we want to track. This is used to do
    some really simplistic filtering on noisy Internet Archive results if we
    aren't filtering down to a explicit list of URLs.
    """
    return (version.mime_type not in SUBRESOURCE_MIME_TYPES and
            splitext(urlparse(version.url).path)[1] not in SUBRESOURCE_EXTENSIONS)


# TODO: this should probably be a method on db.Client, but db.Client could also
# do well to transform the `links` into callables, e.g:
#     more_pages = pages['links']['next']()
def _list_all_db_pages(client, url_pattern=None):
    chunk = 1
    while chunk > 0:
        pages = client.list_pages(sort=['created_at:asc'], chunk_size=1000,
                                  chunk=chunk, url=url_pattern, active=True)
        yield from pages['data']
        chunk = pages['links']['next'] and (chunk + 1) or -1


def _parse_date_argument(date_string):
    """Parse a CLI argument that should represent a date into a datetime"""
    if not date_string:
        return None

    try:
        hours = float(date_string)
        return datetime.utcnow() - timedelta(hours=hours)
    except ValueError:
        pass

    try:
        parsed = pandas.to_datetime(date_string)
        if not pandas.isnull(parsed):
            return parsed
    except ValueError:
        pass

    return None


def main():
    doc = f"""Command Line Interface to the web_monitoring Python package

Usage:
wm import ia <url> [--from <from_date>] [--to <to_date>] [--tag <tag>...] [--maintainer <maintainer>...] [options]
wm import ia-known-pages [--from <from_date>] [--to <to_date>] [--pattern <url_pattern>] [--tag <tag>...] [--maintainer <maintainer>...] [options]
wm db list-domains [--pattern <url_pattern>]

Options:
-h --help                     Show this screen.
--version                     Show version.
--maintainer <maintainer>     Name of entity that maintains the imported pages.
                              Repeat to add multiple maintainers.
--tag <tag>                   Tags to apply to pages. Repeat for multiple tags.
--skip-unchanged <skip_type>  Skip consecutive captures of the same content.
                              Can be:
                                `none` (no skipping),
                                `response` (if the response is unchanged), or
                                `resolved-response` (if the final response
                                    after redirects is unchanged)
                              [default: resolved-response]
--pattern <url_pattern>       A pattern to match when retrieving URLs from a
                              web-monitoring-db instance.
--parallel <parallel_count>   Number of parallel network requests to support.
                              [default: {PARALLEL_REQUESTS}]
--unplaybackable <play_path>  A file in which to list memento URLs that can not
                              be played back. When importing is complete, a
                              list of unplaybackable mementos will be written
                              to this file. If it exists before importing,
                              memento URLs listed in it will be skipped.
--dry-run                     Don't upload data to web-monitoring-db.
"""
    arguments = docopt(doc, version='0.0.1')
    command = None
    if arguments['import']:
        skip_unchanged = arguments['--skip-unchanged']
        if skip_unchanged not in ('none', 'response', 'resolved-response'):
            print('--skip-unchanged must be one of `none`, `response`, '
                  'or `resolved-response`')
            return

        if arguments['ia']:
            command = import_ia_urls(
                urls=[arguments['<url>']],
                maintainers=arguments.get('--maintainer'),
                tags=arguments.get('--tag'),
                from_date=_parse_date_argument(arguments['<from_date>']),
                to_date=_parse_date_argument(arguments['<to_date>']),
                skip_unchanged=skip_unchanged,
                unplaybackable_path=arguments.get('--unplaybackable'),
                dry_run=arguments.get('--dry-run'))
        elif arguments['ia-known-pages']:
            command = import_ia_db_urls(
                from_date=_parse_date_argument(arguments['<from_date>']),
                to_date=_parse_date_argument(arguments['<to_date>']),
                maintainers=arguments.get('--maintainer'),
                tags=arguments.get('--tag'),
                skip_unchanged=skip_unchanged,
                url_pattern=arguments.get('--pattern'),
                worker_count=int(arguments.get('--parallel')),
                unplaybackable_path=arguments.get('--unplaybackable'),
                dry_run=arguments.get('--dry-run'))

    elif arguments['db']:
        if arguments['list-domains']:
            list_domains(url_pattern=arguments.get('--pattern'))

    # Start a loop and execute commands that are async.
    if asyncio.iscoroutine(command):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(command)


if __name__ == '__main__':
    main()
