import logging
from unittest.mock import MagicMock

import pytest
from mock import MagicMock, call
from user_sync.error import AssertionException

from user_sync.config.sign_sync import SignConfigLoader
from user_sync.engine.sign import SignSyncEngine
from user_sync.engine.umapi import AdobeGroup


@pytest.fixture
def example_engine(default_sign_args):
    config = SignConfigLoader(default_sign_args)
    rule_config = config.get_engine_options()
    target_options = config.get_target_options()
    return SignSyncEngine(rule_config, target_options)

@pytest.fixture
def mock_data_for_update():
    from sign_client.model import UserGroupInfo
    return {
        'groups_to_update': {},
        'assigned_groups': {
            'group1': UserGroupInfo(id='1', name='group1', isGroupAdmin=False, isPrimaryGroup=True, status='ACTIVE'),
            'group2': UserGroupInfo(id='2', name='group2', isGroupAdmin=True, isPrimaryGroup=False, status='ACTIVE')
        },
        'desired_groups': ['group1', 'group2'],
        'admin_groups': ['group1'],
        'directory_user': {'is_group_admin': True},
        'sign_user': MagicMock(email='user@example.com')
    }

@pytest.fixture
def mock_data_for_insert_user():
    return {
        'org_name': 'org1',
        'directory_user': {
            'email': 'user@example.com',
            'is_admin': False,
            'firstname': 'First',
            'lastname': 'Last',
            'is_group_admin': True,
            'admin_groups': [MagicMock(group_name='group1')]
        },
        'assignment_groups': [MagicMock(group_name='group1'), MagicMock(group_name='group2')]
    }

@pytest.fixture
def setup_data_resolve_group_mapping():
    return {
        'directory_groups': ['group1'],
        'group_mapping': {
            'group1': {'priority': 1, 'groups': ['mapped_group1', 'mapped_group2']},
        },
        'account_admin_groups': ['group1'],
        'group_admin_mapping': {
            'group1': {'mapped_group1'},
        }
    }



def test_load_users_and_groups(example_engine: SignSyncEngine, mock_dir_user):
    dc = MagicMock()
    mock_dir_user['groups'] = ["Sign Users 1"]
    user = {'user1@example.com': mock_dir_user}

    def dir_user_replacement(*args, **kwargs):
        return user.values()

    dc.load_users_and_groups = dir_user_replacement
    mapping = {}
    AdobeGroup.index_map = {}
    adobe_groups = [AdobeGroup('Group 1', 'primary')]
    mapping['Sign Users'] = {'groups': adobe_groups}
    example_engine.read_desired_user_groups(mapping, dc)
    assert example_engine.directory_user_by_user_key == user


def test_get_directory_user_key(example_engine, mock_dir_user):
    # if the method is passed a dict with an email, it should return the email key
    assert example_engine.get_directory_user_key(
        mock_dir_user) == mock_dir_user['email']
    # if the user object passed in has no email value, it should return None
    assert example_engine.get_directory_user_key(
        {'': {'username': 'user1@example.com'}}) is None


def test_handle_sign_only_users(example_engine):
    from sign_client.model import DetailedUserInfo, GroupInfo, UserGroupInfo
    ex_sign_user = DetailedUserInfo(
        accountType='GLOBAL',
        email='user@example.com',
        id='12345',
        isAccountAdmin=True,
        firstName='Test',
        lastName='User',
        initials='TU',
        locale='en_us',
        accountId='9876',
        status='ACTIVE',
    )

    sign_connector = MagicMock()
    example_engine.sign_only_users_by_org['primary'] = {'example.user@signtest.com': ex_sign_user}
    example_engine.default_groups['primary'] = GroupInfo(
        groupId='abc12345',
        groupName='Default Group',
        createdDate="6 o'clock",
        isDefaultGroup=True,
    )
    example_engine.sign_user_groups['primary'] = {ex_sign_user.id: [UserGroupInfo(
        id='xyz98765',
        isGroupAdmin=True,
        isPrimaryGroup=True,
        status='ACTIVE',
    )]}

    # Check exclude action
    example_engine.options['user_sync']['sign_only_user_action'] = 'exclude'
    example_engine.handle_sign_only_users(sign_connector, 'primary')
    assert sign_connector.deactivate_user.call_args is None
    assert sign_connector.update_users.call_args == call([])

    # Check reset (groups and roles)
    example_engine.options['user_sync']['sign_only_user_action'] = 'reset'
    example_engine.handle_sign_only_users(sign_connector, 'primary')
    assert sign_connector.update_users.call_args[0][0][0].isAccountAdmin is False
    assert sign_connector.update_user_groups.call_args[0][0][0][1].groupInfoList[0].id == 'abc12345'
    assert sign_connector.update_user_groups.call_args[0][0][0][1].groupInfoList[0].isGroupAdmin is False

    # Check remove_roles (group should remain the same as it is for ex_sign_user)
    example_engine.options['user_sync']['sign_only_user_action'] = 'remove_roles'
    example_engine.handle_sign_only_users(sign_connector, 'primary')
    assert sign_connector.update_users.call_args[0][0][0].isAccountAdmin is False
    assert sign_connector.update_user_groups.call_args[0][0][0][1].groupInfoList[0].id == 'xyz98765'
    assert sign_connector.update_user_groups.call_args[0][0][0][1].groupInfoList[0].isGroupAdmin is False

    # Check remove_groups (role should remain the same as it is for ex_sign_user)
    example_engine.options['user_sync']['sign_only_user_action'] = 'remove_groups'
    example_engine.handle_sign_only_users(sign_connector, 'primary')
    assert sign_connector.update_user_groups.call_args[0][0][0][1].groupInfoList[0].id == 'abc12345'

def test_roles_match():
    resolved_role = ['GROUP_ADMIN', 'ACCOUNT_ADMIN']
    sign_role = ['ACCOUNT_ADMIN', 'GROUP_ADMIN']
    assert SignSyncEngine.roles_match(resolved_role, sign_role)
    assert not SignSyncEngine.roles_match(resolved_role, [])

    resolved_roles = ['ACCOUNT_ADMIN', 'GROUP_ADMIN', 'NORMAL_USER']
    sign_roles = ['ACCOUNT_ADMIN', 'GROUP_ADMIN', 'NORMAL_USER']
    assert SignSyncEngine.roles_match(resolved_roles, sign_roles) is True

    resolved_roles = ['GROUP_ADMIN', 'NORMAL_USER', 'ACCOUNT_ADMIN']
    sign_roles = ['ACCOUNT_ADMIN', 'GROUP_ADMIN', 'NORMAL_USER']
    assert SignSyncEngine.roles_match(resolved_roles, sign_roles) is True

    resolved_roles = []
    sign_roles = []
    assert SignSyncEngine.roles_match(resolved_roles, sign_roles) is True

    resolved_roles = ['normal_user']
    sign_roles = ['NORMAL_USER']
    assert SignSyncEngine.roles_match(resolved_roles, sign_roles) is False

    resolved_roles = ['NORMAL_USER', 'ACCOUNT_ADMIN']
    sign_roles = ['GROUP_ADMIN', 'NORMAL_USER']
    assert SignSyncEngine.roles_match(resolved_roles, sign_roles) is False


def test_should_sync():
    AdobeGroup.index_map = {}
    dir_user = {'sign_groups': [AdobeGroup.create('test group')]}
    assert SignSyncEngine.should_sync(dir_user, None)
    assert not SignSyncEngine.should_sync(dir_user, 'secondary')


def test__groupify():
    AdobeGroup.index_map = {}
    g1 = AdobeGroup.create('Sign Group 1')
    g2 = AdobeGroup.create('Sign Group 2')
    g3 = AdobeGroup.create('sec::Sign Group 3')

    processed_groups = SignSyncEngine._groupify(None, [{'groups': [g1, g2, g3]}])
    assert processed_groups == ['Sign Group 1', 'Sign Group 2']
    processed_groups = SignSyncEngine._groupify("sec", [{'groups': [g1, g2, g3]}])
    assert processed_groups == ['Sign Group 3']


def test_read_desired_user_groups(example_engine):
    directory_connector = MagicMock()
    g1 = AdobeGroup.create('Sign Group 1')
    g2 = AdobeGroup.create('Sign Group 2')
    g3 = AdobeGroup.create('Sign Group 3')

    mappings = {
        'Sign Group 1': {
            'priority': 0,
            'roles': set(),
            'groups': [g1]
        },
        'Test Group Admins 1': {
            'priority': 4,
            'roles': {'GROUP_ADMIN'},
            'groups': []
        },
        'Sign Group 2': {
            'priority': 2,
            'roles': set(),
            'groups': [g2, g1, g3]
        },
        'Test Group Admins 2': {
            'priority': 1,
            'roles': {'ACCOUNT_ADMIN'},
            'groups': []
        },
        'Sign Group 3': {
            'priority': 3,
            'roles': set(),
            'groups': [g3]
        },
        'Test Group Admins 3': {
            'priority': 5,
            'roles': {'ACCOUNT_ADMIN', 'GROUP_ADMIN'},
            'groups': [g2]
        },
    }
    example_engine.read_desired_user_groups(mappings, directory_connector)

    assert mappings['Sign Group 1']['priority'] == 0
    assert mappings['Test Group Admins 3']['roles'] == {'ACCOUNT_ADMIN', 'GROUP_ADMIN'}


def test_read_desired_user_groups_simple(example_engine, mock_dir_user):
    dc = MagicMock()
    mock_dir_user['groups'] = ["Sign Users 1"]
    user = {'user1@example.com': mock_dir_user}

    def dir_user_replacement(*args, **kwargs):
        return user.values()

    dc.load_users_and_groups = dir_user_replacement
    mapping = {}
    AdobeGroup.index_map = {}
    adobe_groups = [AdobeGroup('Group 1', 'primary')]
    mapping['Sign Users'] = {'groups': adobe_groups}
    example_engine.read_desired_user_groups(mapping, dc)
    assert example_engine.directory_user_by_user_key == user

def test_resolve_primary_group_and_group_admin_state_umg_true(example_engine, mock_data_for_update):
    example_engine.resolve_primary_group = MagicMock(return_value='group2')
    groups_to_update = example_engine.resolvePrimaryGroupAndGroupAdminState(
        is_umg=True,
        groups_to_update=mock_data_for_update['groups_to_update'],
        assigned_groups=mock_data_for_update['assigned_groups'],
        desired_groups=mock_data_for_update['desired_groups'],
        admin_groups=mock_data_for_update['admin_groups'],
        directory_user=mock_data_for_update['directory_user'],
        sign_user=mock_data_for_update['sign_user']
    )
    assert 'group2' in groups_to_update
    assert groups_to_update['group2'].isPrimaryGroup is True

def test_resolve_primary_group_and_group_admin_state_no_primary_group(example_engine, mock_data_for_update):
    example_engine.resolve_primary_group = MagicMock(return_value=None)
    with pytest.raises(AssertionException, match="Can't identify a primary group for user 'user@example.com'"):
        example_engine.resolvePrimaryGroupAndGroupAdminState(
            is_umg=True,
            groups_to_update=mock_data_for_update['groups_to_update'],
            assigned_groups=mock_data_for_update['assigned_groups'],
            desired_groups=mock_data_for_update['desired_groups'],
            admin_groups=mock_data_for_update['admin_groups'],
            directory_user=mock_data_for_update['directory_user'],
            sign_user=mock_data_for_update['sign_user']
        )

def test_resolve_primary_group_and_group_admin_state_update_group_admin(example_engine, mock_data_for_update):
    groups_to_update = example_engine.resolvePrimaryGroupAndGroupAdminState(
        is_umg=False,
        groups_to_update=mock_data_for_update['groups_to_update'],
        assigned_groups=mock_data_for_update['assigned_groups'],
        desired_groups=mock_data_for_update['desired_groups'],
        admin_groups=mock_data_for_update['admin_groups'],
        directory_user=mock_data_for_update['directory_user'],
        sign_user=mock_data_for_update['sign_user']
    )

    assert 'group1' in groups_to_update
    assert groups_to_update['group1'].isGroupAdmin is True

def test_resolve_primary_group_and_group_admin_state_umg_false(example_engine, mock_data_for_update):
    from sign_client.model import UserGroupInfo
    groups_to_update = example_engine.resolvePrimaryGroupAndGroupAdminState(
        is_umg=False,
        groups_to_update=mock_data_for_update['groups_to_update'],
        assigned_groups= {'group1': UserGroupInfo(id='1', name='group1', isGroupAdmin=False, isPrimaryGroup=True, status='ACTIVE')},
        desired_groups= ['group1'],
        admin_groups=['group1'],
        directory_user=mock_data_for_update['directory_user'],
        sign_user=mock_data_for_update['sign_user']
    )

    assert 'group1' in groups_to_update
    assert groups_to_update['group1'].isGroupAdmin is True


def test_insert_new_users_umg_primary_group(example_engine, mock_data_for_insert_user):
    from sign_client.model import GroupInfo
    example_engine.options['user_sync']['umg'] = True
    example_engine.resolve_primary_group = MagicMock(return_value='group1')
    example_engine.sign_groups = {
        'org1': {
            'group1': GroupInfo(groupId='1', groupName='group1'),
            'group2': GroupInfo(groupId='2', groupName='group2')
        } }
    sign_connector = MagicMock()
    example_engine.insert_new_users(
        org_name=mock_data_for_insert_user['org_name'],
        sign_connector=sign_connector,
        directory_user=mock_data_for_insert_user['directory_user'],
        assignment_groups=mock_data_for_insert_user['assignment_groups']
    )

    assert example_engine.sign_users_created == {'user@example.com'}

def test_insert_new_users_non_umg_primary_group(example_engine, mock_data_for_insert_user):
    from sign_client.model import GroupInfo
    example_engine.options['user_sync']['umg'] = False
    example_engine.resolve_primary_group = MagicMock(return_value='group1')
    example_engine.sign_groups = {
        'org1': {
            'group1': GroupInfo(groupId='1', groupName='group1'),
            'group2': GroupInfo(groupId='2', groupName='group2')
        } }
    sign_connector = MagicMock()
    example_engine.insert_new_users(
        org_name=mock_data_for_insert_user['org_name'],
        sign_connector=sign_connector,
        directory_user=mock_data_for_insert_user['directory_user'],
        assignment_groups=mock_data_for_insert_user['assignment_groups']
    )

    assert example_engine.sign_users_created == {'user@example.com'}

def test_resolve_group_mappings(setup_data_resolve_group_mapping):
    directory_groups = setup_data_resolve_group_mapping['directory_groups']
    group_mapping = setup_data_resolve_group_mapping['group_mapping']
    account_admin_groups = setup_data_resolve_group_mapping['account_admin_groups']
    group_admin_mapping = setup_data_resolve_group_mapping['group_admin_mapping']

    matched_groups, is_admin, is_group_admin, admin_groups = SignSyncEngine.resolve_group_mappings(
        directory_groups, group_mapping, account_admin_groups, group_admin_mapping
    )

    assert set(matched_groups) == {'mapped_group1', 'mapped_group2'}
    assert is_admin is True
    assert is_group_admin is True
    assert admin_groups == {'mapped_group1'}

def test_group_admin_mapping(setup_data_resolve_group_mapping):
    directory_groups = setup_data_resolve_group_mapping['directory_groups']
    group_mapping = setup_data_resolve_group_mapping['group_mapping']
    account_admin_groups =  ['group2']
    group_admin_mapping = setup_data_resolve_group_mapping['group_admin_mapping']

    matched_groups, is_admin, is_group_admin, admin_groups = SignSyncEngine.resolve_group_mappings(
        directory_groups, group_mapping, account_admin_groups, group_admin_mapping
    )

    assert set(matched_groups) == {'mapped_group1', 'mapped_group2'}
    assert is_admin is False
    assert is_group_admin is True
    assert admin_groups == {'mapped_group1'}


@pytest.mark.skip("wait until UMG is implemented")
def test_extract_mapped_group():
    AdobeGroup.index_map = {}

    def check_mapping(user_groups, group, roles):
        res = SignSyncEngine.extract_mapped_group(user_groups, mappings)
        if group is None:
            assert res['group'] is None
        else:
            assert AdobeGroup.create(group) == res['group']
        for r in roles:
            assert r in res['roles']

    g1 = AdobeGroup.create('Sign Group 1')
    g2 = AdobeGroup.create('Sign Group 2')
    g3 = AdobeGroup.create('Sign Group 3')

    mappings = {
        'Sign Group 1': {
            'priority': 0,
            'roles': set(),
            'groups': [g1]
        },
        'Test Group Admins 1': {
            'priority': 4,
            'roles': {'GROUP_ADMIN'},
            'groups': []
        },
        'Sign Group 2': {
            'priority': 2,
            'roles': set(),
            'groups': [g2, g1, g3]
        },
        'Test Group Admins 2': {
            'priority': 1,
            'roles': {'ACCOUNT_ADMIN'},
            'groups': []
        },
        'Sign Group 3': {
            'priority': 3,
            'roles': set(),
            'groups': [g3]
        },
        'Test Group Admins 3': {
            'priority': 5,
            'roles': {'ACCOUNT_ADMIN', 'GROUP_ADMIN'},
            'groups': [g2]
        },
    }

    check_mapping([], None, ['NORMAL_USER'])
    check_mapping(['Not A Group'], None, ['NORMAL_USER'])
    check_mapping(['Sign Group 1'], 'Sign Group 1', ['NORMAL_USER'])
    check_mapping(['Test Group Admins 1'], None, ['GROUP_ADMIN'])
    check_mapping(['Test Group Admins 3'], 'Sign Group 2', ['ACCOUNT_ADMIN', 'GROUP_ADMIN'])
    check_mapping(['Sign Group 1', 'Test Group Admins 1'], 'Sign Group 1', ['GROUP_ADMIN'])
    check_mapping(['Sign Group 1', 'Sign Group 2'], 'Sign Group 1', ['NORMAL_USER'])
    check_mapping(['Sign Group 3', 'Sign Group 2'], 'Sign Group 2', ['NORMAL_USER'])
    check_mapping(['Sign Group 3', 'Test Group Admins 1', 'Test Group Admins 2'],
                  'Sign Group 3', ['ACCOUNT_ADMIN', 'GROUP_ADMIN'])
