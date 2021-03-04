import asyncio
import json
import logging
import time
from math import ceil

import aiohttp
import requests

from user_sync.error import AssertionException


class SignClient:
    version = 'v5'
    _endpoint_template = 'api/rest/{}/'
    DEFAULT_GROUP_NAME = 'default group'

    def __init__(self, config):
        for k in ['host', 'key', 'admin_email']:
            if k not in config:
                raise AssertionException("Key '{}' must be specified for all Sign orgs".format(k))
        self.host = config['host']
        self.key = config['key']
        self.admin_email = config['admin_email']
        self.console_org = config['console_org'] if 'console_org' in config else None
        self.api_url = None
        self.groups = None
        self.max_sign_retries = 3
        self.sign_timeout = 120
        self.concurrency_limit = config.get('request_concurrency') or 1
        self.batch_size = config.get('batch_size') or 10000
        self.logger = logging.getLogger(self.logger_name())
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        self.loop = asyncio.get_event_loop()
        self.users = {}
        self.fetched_count = 0
        self.updated_count = 0

    def _init(self):
        self.api_url = self.base_uri()
        self.groups = self.get_groups()
        self.reverse_groups = {v: k for k, v in self.groups.items()}

    def sign_groups(self):
        if self.api_url is None or self.groups is None:
            self._init()
        return self.groups

    def logger_name(self):
        return 'sign_client.{}'.format(self.console_org if self.console_org else 'main')

    def header(self):
        """
        Return Sign API auth header
        :return: dict()
        """
        if self.version == 'v6':
            return {
                'Authorization': "Bearer {}".format(self.key),
                'Connection': 'close',
            }
        return {
            'Access-Token': self.key,
        }

    def header_json(self):
        """
        Get auth headers with options to PUT/POST JSON
        :return: dict()
        """

        json_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        json_headers.update(self.header())
        return json_headers

    def base_uri(self):
        """
        This function validates that the SIGN integration key is valid.
        :return: dict()
        """

        endpoint = self._endpoint_template.format(self.version)
        url = 'https://' + self.host + '/' + endpoint

        if self.version == 'v6':
            url_path = 'baseUris'
            access_point_key = 'apiAccessPoint'
        else:
            url_path = 'base_uris'
            access_point_key = 'api_access_point'

        result = requests.get(url + url_path, headers=self.header())
        if result.status_code != 200:
            raise AssertionException('Error getting base URI from Sign API, is API key valid?')

        if access_point_key not in result.json():
            raise AssertionException('Error getting base URI for Sign API, result invalid')
        self.logger.debug('base_uri result: {}'.format(result.json()[access_point_key] + endpoint))

        return result.json()[access_point_key] + endpoint

    def get_groups(self):
        """
        API request to get group information
        :return: dict()
        """
        if self.api_url is None:
            self.api_url = self.base_uri()
        url = self.api_url + 'groups'
        header = self.header()
        sign_groups, code = self.call_with_retry_sync('GET', url, header)
        self.logger.info('getting Sign user groups')
        groups = {}
        for group in sign_groups['groupInfoList']:
            groups[group['groupName'].lower()] = group['groupId']
        return groups

    def create_group(self, group):
        """
        Create a new group in Sign
        :param group: str
        :return:
        """
        if self.api_url is None or self.groups is None:
            self._init()
        url = self.api_url + 'groups'
        header = self.header_json()
        data = json.dumps({'groupName': group})
        self.logger.info('Creating Sign group {} '.format(group))
        res, code = self.call_with_retry_sync('POST', url, header, data)
        self.groups[group] = res['groupId']

    def update_users(self, users):
        """
        Need to define this method, so that it can be called outside async context
        return dict so as to comply with existing code calling this function
        *prefer instance var to awaiting coroutine results as we cannot
        guarantee they will be user obj
        """
        if self.api_url is None or self.groups is None:
            self._init()
        t0 = time.perf_counter()

        if not users:
            return

        nbat = ceil(len(users) / self.batch_size)
        for i in range(0, len(users), self.batch_size):
            self.logger.info("Batch # {}/{}".format(i + 1, nbat))
            self.loop.run_until_complete(self.update_users_async(users[i:i + self.batch_size]))
        self.logger.info("Update time: {}".format(time.perf_counter() - t0))

    def get_users(self):
        """
        Need to define this method, so that it can be called outside async context
        return dict so as to comply with existing code calling this function
        *prefer instance var to awaiting coroutine results as we cannot
        guarantee they will be user obj
        """
        t0 = time.perf_counter()

        if self.api_url is None or self.groups is None:
            self._init()

        headers = self.header()
        users_url = self.api_url + 'users'
        self.logger.info('getting list of all Sign users')

        user_list, code = self.call_with_retry_sync('GET', users_url, headers)
        user_list = user_list['userInfoList']

        nbat = ceil(len(user_list) / self.batch_size)
        for i in range(0, len(user_list), self.batch_size):
            self.logger.info("Batch # {}/{}".format(i + 1, nbat))
            self.loop.run_until_complete(self.get_users_async(user_list[i:i + self.batch_size]))
        self.logger.info("Get time: {}".format(time.perf_counter() - t0))
        return self.users

    async def get_users_async(self, users):
        """
        Get list of all users from Sign (indexed by email address)
        """

        # Semaphore specifies number of allowed calls at one time
        sem = asyncio.Semaphore(value=self.concurrency_limit)

        # We must use only 1 session, else will hang
        async with aiohttp.ClientSession(trust_env=True) as session:
            # prepare a list of calls to make * Note: calls are prepared by using call
            # syntax (eg, func() and not func), but they will not be run until executed by the wait
            # split into batches of self.bach_size to avoid taking too much memory
            headers = self.header()
            calls = [self._get_user(sem, u['userId'], headers, session) for u in users]
            await asyncio.wait(calls)

    async def update_users_async(self, users):
        """
        Get list of all users from Sign (indexed by email address)
        """

        # Semaphore specifies number of allowed calls at one time
        sem = asyncio.Semaphore(value=self.concurrency_limit)

        # We must use only 1 session, else will hang
        async with aiohttp.ClientSession(trust_env=True) as session:
            # prepare a list of calls to make * Note: calls are prepared by using call
            # syntax (eg, func() and not func), but they will not be run until executed by the wait
            headers = self.header_json()
            calls = [self._update_user(sem, u, headers, session) for u in users]
            await asyncio.wait(calls)

    async def _get_user(self, semaphore, user_id, header, session):

        # This will block the method from executing until a position opens
        async with semaphore:
            user_url = self.api_url + 'users/' + user_id
            user, code = await self.call_with_retry_async('GET', user_url, header, session=session)
            self.fetched_count += 1
            self.logger.info("Total retrieved: {}".format(self.fetched_count))
            if code != 200:
                self.logger.error("Error fetching user '{}' with response: {}".format(user_id, user))
                return
            if user['userStatus'] != 'ACTIVE':
                return
            if user['email'] == self.admin_email:
                return
            user['userId'] = user_id
            user['roles'] = self.user_roles(user)
            self.users[user['email']] = user
            self.logger.debug('retrieved user details for Sign user {}'.format(user['email']))

    async def _update_user(self, semaphore, user, headers, session):
        """
        Update Sign user
        :param user_id: str
        :param data: dict()
        :return: dict()
        """

        async with semaphore:
            url = self.api_url + 'users/' + user['userId']
            group = self.reverse_groups[user['groupId']]
            body, code = await self.call_with_retry_async('PUT', url, headers, data=json.dumps(user), session=session)
            self.updated_count += 1
            self.logger.info("Total updated: {}".format(self.updated_count))
            self.logger.info(
                "Updated Sign user '{}', Group: '{}', Roles: {}".format(user['email'], group, user['roles']))
            if code != 200:
                self.logger.error("Error updating user '{}' with response: {}".format(user['email'], body))

    @staticmethod
    def user_roles(user):
        """
        Resolve user roles
        :return: list[]
        """
        return ['NORMAL_USER'] if 'roles' not in user else user['roles']

    def call_with_retry_sync(self, method, url, header, data=None):
        """
        Need to define this method, so that it can be called outside async context
        loop will execute a single synchronous call, but sharing code with the async retry method
        """
        return self.loop.run_until_complete(self.call_with_retry_async(method, url, header, data=data or {}))

    async def call_with_retry_async(self, method, url, header, data=None, session=None):
        """
        Call manager with exponential retry
        :return: Response <Response> object
        """
        retry_nb = 1
        waiting_time = 10
        close = session is None
        session = session or aiohttp.ClientSession(trust_env=True)
        session.headers.update(header)
        while True:
            try:
                waiting_time *= 3
                self.logger.debug('Attempt {} to call: {}'.format(retry_nb, url))
                async with session.request(method=method, url=url, data=data or {}) as r:
                    if r.status >= 500:
                        raise Exception('{}, Headers: {}'.format(r.status, r.headers))
                    elif r.status == 429:
                        raise Exception('{} - too many calls. Headers: {}'.format(r.status, r.headers))
                    elif r.status > 400 and r.status < 500:
                        self.logger.critical(' {} - {}. Headers: {}'.format(r.status, r.reason, r.headers))
                        raise AssertionException('')
                    body = await r.json()
                    return body, r.status
            except Exception as exp:
                self.logger.warning('Failed: {} - {}'.format(type(exp), exp.args))
                if retry_nb == (self.max_sign_retries + 1):
                    raise AssertionException('Quitting after {} retries'.format(self.max_sign_retries))
                self.logger.warning('Waiting for {} seconds'.format(waiting_time))
                await asyncio.sleep(waiting_time)
                self.logger.warning("Retrying...")
                retry_nb += 1
            finally:
                if close:
                    await session.close()
