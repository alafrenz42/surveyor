import configparser
import json
import logging
import os
import time
from tqdm import tqdm
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple, Callable

import requests
from requests.adapters import HTTPAdapter

from common import Product, Tag, Result
from help import datetime_to_epoch_millis


@dataclass
class Query:
    start_date: datetime
    end_date: datetime
    parameter: Optional[str]
    operator: Optional[str]
    search_value: Optional[str]
    full_query: Optional[str] = None


PARAMETER_MAPPING: dict[str, str] = {
    'process_name': 'ProcessName',
    'ipaddr': 'IP',
    'cmdline': 'CmdLine',
    'digsig_publisher': 'SrcProcPublisher',
    'domain': 'Url',
    'internal_name': 'TgtFileInternalName'
}


class SentinelOne(Product):
    """
    Surveyor implementation for product "SentinelOne"
    """
    product: str = 's1'
    creds_file: str  # path to credential configuration file
    _token: str  # AAD access token
    _url: str  # URL of SentinelOne console
    _site_id: Optional[str]  # Site ID for SentinelOne
    _account_id: Optional[str]  # Account ID for SentinelOne
    _session: requests.Session
    _queries: dict[Tag, list[Query]]
    _last_request: float
    _site_ids: list[str]

    def __init__(self, profile: str, creds_file: str, account_id: Optional[list[str]] = None,
                 site_id: Optional[list[str]] = None, account_name: Optional[list[str]] = None, **kwargs):
        if not os.path.isfile(creds_file):
            raise ValueError(f'Credential file {creds_file} does not exist')

        self.creds_file = creds_file
        self._queries = dict()

        self._last_request = 0.0

        super().__init__(self.product, profile, **kwargs)

        config = configparser.ConfigParser()
        config.read(self.creds_file)

        # instantiate site_ids and account_ids if not set
        site_ids = site_id if site_id else list()
        account_ids = account_id if account_id else list()
        account_names = account_name if account_name else list()

        # extract account/site ID from configuration if set
        if 'account_id' in config[profile] and config[profile]['account_id'] not in account_ids:
            account_ids.append(config[profile]['account_id'])

        if 'site_id' in config[profile] and config[profile]['site_id'] not in site_ids:
            site_ids.append(config[profile]['site_id'])

        if 'account_name' in config[profile] and config[profile]['account_name'] not in account_names:
            account_names.append(config[profile]['account_name'])

        # determine site IDs to query (default is all)
        self._site_ids = site_ids

        # ensure specified site IDs are valid
        site_response_data = self._get_all_paginated_data(self._build_url('/web/api/v2.1/sites'),
                                                          params={'siteIds': ','.join(site_ids)},
                                                          add_default_params=False)
        existing_site_ids = set[int]()
        for response in site_response_data:
            for site in response['sites']:
                existing_site_ids.add(site['id'])

        for scope_id in self._site_ids:
            if scope_id not in existing_site_ids:
                raise ValueError(f'Site with ID {scope_id} does not exist')

        # get site IDs for each specified account id
        for scope_id in account_ids:
            for response in self._get_all_paginated_data(self._build_url('/web/api/v2.1/sites'),
                                                         params={'accountId': scope_id},
                                                         add_default_params=False):
                for site in response['sites']:
                    if site['id'] not in self._site_ids:
                        self._site_ids.append(site['id'])

        for name in account_names:
            for response in self._get_all_paginated_data(self._build_url('/web/api/v2.1/sites'),
                                                         params={'name': name},
                                                         add_default_params=False):
                for site in response['sites']:
                    if site['id'] not in self._site_ids:
                        self._site_ids.append(site['id'])

        self.log.debug(f'Site IDs: {self._site_ids}')

    def _authenticate(self):
        config = configparser.ConfigParser()
        config.read(self.creds_file)

        if self.profile not in config:
            raise ValueError(f'Profile {self.profile} is not present in credential file')

        section = config[self.profile]

        # ensure configuration has required fields
        if 'url' not in section:
            raise ValueError(f'S1 configuration invalid, ensure "url" is specified')

        if 'site_id' not in section and 'account_id' not in section:
            raise ValueError(f'S1 configuration invalid, specify a site_id or account_id')

        # extract required information from configuration
        if 'token' in section:
            self._token = section['token']
        else:
            if 'S1_TOKEN' not in os.environ:
                raise ValueError(f'S1 configuration invalid, specify "token" configuration value or "S1_TOKEN" '
                                 f'environment variable')
            self._token = os.environ['S1_TOKEN']

        self._site_id = section['site_id'] if 'site_id' in section else None
        self._account_id = section['account_id'] if 'account_id' in section else None

        self._url = section['url'].rstrip('/')

        if not self._url.startswith('https://'):
            raise ValueError(f'URL must start with "https://"')

        # create a session and a pooled HTTPAdapter
        self._session = requests.session()
        self._session.mount('https://', HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=3))

        # test API key by retrieving the sensor count, which is a fast operation
        data = self._session.get(self._build_url('/web/api/v2.1/agents/count'),
                                 headers=self._get_default_header(),
                                 params=self._get_default_body()).json()
        if 'errors' in data:
            if data['errors'][0]['code'] == 4010010:
                raise ValueError(f'Failed to authenticate to SentinelOne: {data}')
            else:
                raise ValueError(f'Error when authenticating to SentinelOne: {data}')

    def _build_url(self, stem: str):
        """
        Assemble URL for SentinelOne API query using base URI and URI stem.
        """
        if not stem.startswith('/'):
            stem = '/' + stem

        return self._url + stem

    def _get_default_body(self) -> dict:
        """
        Get the default request body for a SentinelOne API query.
        """
        return {"siteIds": [self._site_id]} if self._site_id else {"accountIds": [self._account_id]}

    def _get_default_header(self):
        """
        Get the default header for a SentinelOne API query.
        """
        return {"Authorization": f"ApiToken {self._token}", "Content-Type": "application/json"}

    def build_query(self, filters: dict) -> Tuple[str, datetime, datetime]:
        to_date = datetime.utcnow()
        from_date = to_date - timedelta(days=14)

        query_base = ''

        for key, value in filters.items():
            if key == 'days':
                from_date = to_date - timedelta(days=value)
            elif key == 'minutes':
                from_date = to_date - timedelta(minutes=value)
            elif key == 'hostname':
                if query_base:
                    query_base += ' AND '

                query_base += f' EndpointName containscis "{value}"'
            elif key == 'username':
                if query_base:
                    query_base += ' AND '

                query_base += f' UserName containscis "{value}"'
            else:
                self._echo(f'Query filter {key} is not supported by product {self.product}', logging.WARNING)

        # S1 requires the date range to be supplied in the query request, not the query text
        # therefore we return the from/to dates separately
        return query_base, from_date, to_date

    def _get_all_paginated_data(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
                                key: str = 'data', after_request: Optional[Callable] = None, limit: int = 1000,
                                no_progress: bool = True, progress_desc: str = 'Retrieving data',
                                add_default_params: bool = True) -> list[dict]:
        """
        Get and return all paginated data from the response, making additional queries if necessary.
        
        :param url: URL to make GET request to.

        :param params: Additional parameters for GET request

        :param limit: Number of items to query per page.

        :param headers: Additional headers for GET quest.

        :param key: Dictionary key in which result data resides.

        :param after_request: Optional callable that is executed after each pagination request. The callable is
        passed the response to the last API call.

        :param no_progress: Suppress progress bar.

        :param progress_desc: Specify description for progress bar.

        :param add_default_params: Whether _get_default_body() should be added to parameter set.

        :returns: List containing data from all pages.
        """
        if params is None:
            params = dict()

        if add_default_params:
            params.update(self._get_default_body())

        params['limit'] = limit

        if headers is None:
            headers = dict()

        headers.update(self._get_default_header())

        data = list[dict]()
        total: int = 0

        next_cursor = True
        with tqdm(desc=progress_desc, disable=not self._tqdm_echo or no_progress) as p_bar:
            while next_cursor:
                response = self._session.get(url, params=params, headers=headers)

                if after_request:
                    # execute after request callback
                    after_request(response)

                response.raise_for_status()

                call_data = response.json()[key]

                if not isinstance(call_data, list):
                    call_data = [call_data]

                self.log.debug(f'Got {len(call_data)} results in page')
                data.extend(call_data)
                pagination_data = response.json()['pagination']

                # update progress bar
                if pagination_data['totalItems'] > total:
                    total = pagination_data['totalItems']
                    p_bar.reset(total=total)

                p_bar.update(len(call_data))

                next_cursor = pagination_data['nextCursor']
                params['cursor'] = next_cursor

            return data

    def _get_dv_events(self, query_id: str) -> list[dict]:
        """
        Retrieve events associated with a SentinelOne Deep Visibility query ID.
        """
        p_bar = tqdm(desc='Running query', disable=not self._tqdm_echo, total=100)

        try:
            last_progress_status = 0
            while True:
                query_status_response = self._session.get(self._build_url('/web/api/v2.1/dv/query-status'),
                                                          params={'queryId': query_id}, headers=self._get_default_header())
                query_status_response.raise_for_status()
                data = query_status_response.json()['data']

                self.log.debug(str(data))

                p_bar.update(data['progressStatus'] - last_progress_status)
                last_progress_status = data['progressStatus']

                if data['progressStatus'] == 100 or data['responseState'] == 'FAILED':
                    if data['responseState'] == 'FAILED':
                        raise ValueError(f'S1QL query failed with message "{data["responseError"]}"')

                    p_bar.close()

                    return self._get_all_paginated_data(self._build_url('/web/api/v2.1/dv/events'),
                                                        params={'queryId': query_id},
                                                        no_progress=False,
                                                        progress_desc='Retrieving query results')
                else:
                    # query-status endpoint has a one request per second rate limit
                    time.sleep(1)
        except Exception as e:
            p_bar.close()
            raise e

    def process_search(self, tag: Tag, base_query: dict, query: str) -> None:
        build_query, from_date, to_date = self.build_query(base_query)
        query = query + build_query
        self._echo(f'Built Query: {query}')

        if tag not in self._queries:
            self._queries[tag] = list()

        query = Query(from_date, to_date, None, None, None, query)
        self._queries[tag].append(query)

    def nested_process_search(self, tag: Tag, criteria: dict, base_query: dict):
        query_base, from_date, to_date = self.build_query(base_query)

        try:
            for search_field, terms in criteria.items():
                all_terms = ', '.join(f'"{term}"' for term in terms)

                if search_field not in PARAMETER_MAPPING:
                    self._echo(f'Query filter {search_field} is not supported by product {self.product}',
                               logging.WARNING)
                    continue

                parameter = PARAMETER_MAPPING[search_field]
                search_value = all_terms

                if len(terms) > 1:
                    search_value = f'({all_terms})'
                    operator = 'in contains anycase'
                else:
                    operator = 'contains'

                if tag not in self._queries:
                    self._queries[tag] = list()

                self._queries[tag].append(Query(from_date, to_date, parameter, operator, search_value))
        except KeyboardInterrupt:
            self._echo("Caught CTRL-C. Returning what we have...")

    def _process_queries(self):
        """
        Process all cached queries.
        """
        start_date = datetime.utcnow()
        end_date = start_date

        # determine earliest start date
        for tag, queries in self._queries.items():
            for query in queries:
                if query.start_date < start_date:
                    start_date = query.start_date

        try:
            # queries with certain operators can be combined into a more compact query format

            # key is a tuple of the query operator and parameter
            # value is a list of Tuples where each tupe contains the query tag and search value
            combined_queries = dict[Tuple[str, str], list[Tuple[Tag, str]]]()

            # tuple contains tag and full query
            # these chunks will be combined with OR statements and executed
            query_text = list[Tuple[Tag, str]]()

            for tag, queries in self._queries.items():
                for query in queries:
                    if query.operator == 'contains':
                        key = (query.operator, query.parameter)
                        if query.operator not in combined_queries:
                            combined_queries[key] = list()

                        combined_queries[key].append((tag, query.search_value))
                    else:
                        full_query = query.parameter + ' ' + query.operator + ' ' + query.search_value
                        query_text.append((tag, full_query))

            # merge combined queries and add them to query_text
            data: list[Tuple[Tag, str]]
            for (operator, parameter), data in combined_queries.items():
                if operator == 'contains':
                    full_query = f'{parameter} in contains anycase ({", ".join(x[1] for x in data)})'

                    tag = Tag(','.join(tag[0].tag for tag in data), ','.join(tag[0].data for tag in data))
                    query_text.append((tag, full_query))
                else:
                    raise NotImplementedError(f'Combining operator "{operator}" queries is not support')

            # all queries that need to be executed are now in query_text
            # execute queries in chunks
            chunk_size = 10

            # merge queries into one large query and execute it
            for i in range(0, len(query_text), chunk_size):
                # do not chain more than 10 ORs in a S1QL query
                merged_tags = set[Tag]()
                merged_query = ''
                for tag, query in query_text[i:i + chunk_size]:
                    # combine queries with ORs
                    if merged_query:
                        merged_query += ' OR '

                    merged_query += query

                    # add tags to set to de-duplicate
                    merged_tags.add(tag)

                # merge all query tags into a single string
                merged_tag = Tag(','.join(tag.tag for tag in merged_tags), ','.join(tag.data for tag in merged_tags))

                if len(self._site_ids):
                    # restrict query to specified sites
                    # S1QL does not support restricting a query to a specified account ID
                    merged_query = f'SiteID in contains ("' + '", "'.join(self._site_ids) + f'") AND ({merged_query})'

                # build request body for DV API call
                params = self._get_default_body()
                params.update({
                    "fromDate": datetime_to_epoch_millis(start_date),
                    "isVerbose": False,
                    "queryType": ['events'],  # options: 'events', 'procesState'
                    "limit": 20000,
                    "toDate": datetime_to_epoch_millis(end_date),
                    "query": merged_query
                })

                self.log.debug(f'Query params: {params}')

                # ensure we do not submit more than one request every 60 seconds to comply with rate limit
                seconds_sice_last_request = time.time() - self._last_request
                if seconds_sice_last_request < 60:
                    sleep_seconds = 60 - seconds_sice_last_request
                    self.log.debug(f'Sleeping for {sleep_seconds}')
                    time.sleep(sleep_seconds)

                # start deep visibility API call
                query_response = self._session.post(self._build_url('/web/api/v2.1/dv/init-query'),
                                                    headers=self._get_default_header(), data=json.dumps(params))
                self._last_request = time.time()

                body = query_response.json()
                if 'errors' in body and any(('could not parse query' in x['detail'] for x in body['errors'])):
                    raise ValueError(f'S1 could not parse query "{merged_query}"')

                self.log.debug(query_response.json())
                query_response.raise_for_status()

                query_id = body['data']['queryId']
                self.log.info(f'Query ID is {query_id}')

                events = self._get_dv_events(query_id)
                self.log.debug(f'Got {len(events)} events')

                self._results[merged_tag] = list()
                for event in events:
                    hostname = event['endpointName']
                    username = event['srcProcUser']
                    path = event['processImagePath']
                    command_line = event['srcProcCmdLine']
                    additional_data = (event['eventTime'], event['siteId'], event['siteName'])

                    result = Result(hostname, username, path, command_line, additional_data)
                    self._results[merged_tag].append(result)

            self._queries.clear()
        except KeyboardInterrupt:
            self._echo("Caught CTRL-C. Returning what we have . . .")

    def get_results(self, final_call: bool = True) -> dict[Tag, list[Result]]:
        self.log.debug('Entered get_results')

        # process any unprocessed queries
        if final_call and len(self._queries) > 0:
            self.log.debug(f'Executing additional _process_queries')
            self._process_queries()

        return self._results

    def get_other_row_headers(self) -> list[str]:
        return ['Event Time', 'Site ID', 'Site Name']
