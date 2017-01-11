from __future__ import print_function

from getpass import getpass
from json import dumps, loads
from os import getcwd
from os.path import basename
from subprocess import call
from sys import exit
from time import sleep

from pyquery import PyQuery as pq
from requests import Session
from stormpath.client import Client
from stormpath.resources.account import AccountList
from stormpath.resources.application import ApplicationList
from stormpath.resources.account_store_mapping import AccountStoreMappingList
from stormpath.resources.directory import DirectoryList
from stormpath.resources.group import GroupList
from termcolor import colored

from .auth import setup_credentials, init_auth
from .context import set_context, show_context, delete_context
from .status import show_status
from .output import get_logger, prompt
from .resources import get_resource, get_resource_data
from .projects import Project
from .util import store_config_file, which


ATTRIBUTE_MAPS = {
    AccountList: dict(
        username = '--username',
        email = '--email',
        given_name = '--given-name',
        middle_name = '--middle-name',
        surname = '--surname',
        password = '--password',
        status = '--status',
        href = '--href',
    ),
    ApplicationList: dict(
        name = '--name',
        description = '--description',
        href = '--href',
    ),
    AccountStoreMappingList: dict(
        href = '--href',
        account_store = '--href',
        application = '--in-application',
        is_default_account_store = '--is-default-account-store',
        is_default_group_store = '--is-default-group-store',
    ),
    DirectoryList: dict(
        name = '--name',
        description = '--description',
        href = '--href',
    ),
    GroupList: dict(
        name = '--name',
        description = '--description',
        href = '--href',
    ),
}

REQUIRED_ATTRIBUTES = {
    AccountList: dict(
        email = '--email',
        given_name = '--given-name',
        surname = '--surname',
        password = '--password',
    ),
    ApplicationList: dict(
        name = '--name',
    ),
    DirectoryList: dict(
        name = '--name',
    ),
    GroupList: dict(
        name = '--name',
    ),
    AccountStoreMappingList: dict(
        application = '--in-application',
        account_store = '--href',
    )
}

EXTRA_MAPS = {
    ApplicationList: dict(
        create_directory = '--create-directory'
    )
}

SEARCH_ATTRIBUTE_MAPS = {}
for k, v in ATTRIBUTE_MAPS.items():
    v = v.copy()
    v.update(dict(status='--status', q='--query'))
    SEARCH_ATTRIBUTE_MAPS[k] = v

RESOURCE_PRIMARY_ATTRIBUTES = {
    AccountList: ['email', 'href'],
    ApplicationList: ['name', 'href'],
    DirectoryList: ['name', 'href'],
    GroupList: ['name', 'href'],
    AccountStoreMappingList: ['account_store', 'href'],
}


def _prompt_if_missing_parameters(coll, args, only_primary=False):
    required_coll_args = REQUIRED_ATTRIBUTES[type(coll)]
    all_coll_args = ATTRIBUTE_MAPS[type(coll)]

    if 'href' in all_coll_args:
        all_coll_args.pop('href')

    supplied_required_arguments = []
    for arg in required_coll_args.values():
        if arg in args and args[arg]:
            supplied_required_arguments.append(arg)

    if len(supplied_required_arguments) == required_coll_args.values():
        return args

    remaining_coll_args = {k: v for k, v in all_coll_args.items() if v in set(all_coll_args.values()) - set(supplied_required_arguments)}
    if remaining_coll_args:
        get_logger().info('Please enter the following information.  Fields with an asterisk (*) are required.')
        get_logger().info('Fields without an asterisk are optional.')

        for arg in sorted(remaining_coll_args):
            if arg == 'password':
                msg = args['--email']
            else:
                required = '*' if arg in required_coll_args.keys() else ''
                msg = '%s%s' % (arg.replace('_', ' ').capitalize(), required)

            v = prompt(arg, msg)
            args[all_coll_args[arg]] = v
        if type(coll) in EXTRA_MAPS:
            v = prompt(None, 'Create a directory for this application?[Y/n]')
            args['--create-directory'] = v != 'n'

    return args


def _specialized_query(coll, args, maps):
    """Formats the params in the right format before passing
    them to the needed sdk method."""
    json_rep = args.get('--json')
    if json_rep:
        try:
            return loads(json_rep)
        except ValueError as e:
            raise ValueError("Error parsing JSON: %s" % e)

    ctype = type(coll)
    pmap = maps.get(ctype, {})
    params = {}

    for name, opt in pmap.items():
        optval = args.get(opt)
        if optval:
            params[name] = optval

    return params


def _primary_attribute(coll, attrs):
    """Checks to see if the required primary attributes ie. identifiers like
    -n or --name are present. Each Resource can have 2 primary attributes name/email
    and the special attribute href"""
    attr_names = RESOURCE_PRIMARY_ATTRIBUTES[type(coll)]
    attr_values = [attrs.get(n) for n in attr_names if attrs.get(n)]

    if not any(attr_values):
        raise ValueError("Required attribute '{}' not specified.".format(' or '.join(attr_names)))

    return attr_names[0], attr_values[0]


def _gather_resource_attributes(coll, args):
    """Allows using --name/name attributes, ie. with and without the dash."""
    attrs = ATTRIBUTE_MAPS[type(coll)]

    for attr in args.get('<attributes>', []):
        if '=' not in attr:
            raise ValueError('Unknown resource attribute: {}'.format(attr))

        name, value = attr.split('=', 1)
        name = name.replace('-', '_')

        if name not in attrs:
            raise ValueError('Unknown resource attribute: {}'.format(name))

        args[attrs[name]] = value

    return args


def _add_resource_to_groups(resource, args):
    """
    Helper function for adding a resource to a group.  Right now, this is only
    specifically used when adding an Account to a Group.

    :param obj resource: The Stormapth resource which we'll be adding the
        specified Groups to.
    :param dict args: The CLI arguments.
    :rtype: obj
    :returns: The original resource object, with all Groups added -- or None.
    """
    arg_groups = args.get('--groups')

    if arg_groups and hasattr(resource, 'add_group'):
        groups = [g.strip() for g in arg_groups.split(',')]
        for group in groups:
            resource.add_group(group)

        return resource


def _check_account_store_mapping(coll, attrs):
    """Takes care of special create case for account store mappings"""
    if isinstance(coll, AccountStoreMappingList):
        attrs['application'] = {'href': attrs.get('application')}
        attrs['account_store'] = {'href': attrs.get('account_store')}

    return attrs


def list_resources(coll, args):
    """List action: Lists the requested Resource collection."""
    args = _gather_resource_attributes(coll, args)
    q = _specialized_query(coll, args, SEARCH_ATTRIBUTE_MAPS)

    if not isinstance(coll, AccountStoreMappingList):
        if q:
            coll = coll.query(**q)

    for r in coll:
        yield get_resource_data(r)


def create_resource(coll, args):
    """Create action: Creates a Resource."""
    args = _gather_resource_attributes(coll, args)
    _prompt_if_missing_parameters(coll, args)
    attrs = _specialized_query(coll, args, ATTRIBUTE_MAPS)
    attrs = _check_account_store_mapping(coll, attrs)
    attr_name, attr_value = _primary_attribute(coll, attrs)
    extra = _specialized_query(coll, args, EXTRA_MAPS)

    resource = coll.create(attrs, **extra)
    _add_resource_to_groups(resource, args)

    get_logger().info('Resource created.')
    return get_resource_data(resource)


def update_resource(coll, args):
    """Update actions: Updates a Resource.
    Requires an identifier like --name."""
    args = _gather_resource_attributes(coll, args)
    attrs = _specialized_query(coll, args, ATTRIBUTE_MAPS)
    attr_name, attr_value = _primary_attribute(coll, attrs)
    resource = get_resource(coll, attr_name, attr_value)

    for name, value in attrs.items():
        if name == attr_name or name == 'href':
            continue

        setattr(resource, name, value)

    resource.save()
    _add_resource_to_groups(resource, args)

    get_logger().info('Resource updated.')
    return get_resource_data(resource)


def delete_resource(coll, args):
    """Delete action: Deletes a Resource.
    Requires an identifier like --name or --email."""
    args = _gather_resource_attributes(coll, args)
    attrs = _specialized_query(coll, args, ATTRIBUTE_MAPS)
    attr_name, attr_value = _primary_attribute(coll, attrs)
    resource = get_resource(coll, attr_name, attr_value)
    data = get_resource_data(resource)
    force = args.get('--force', False)

    try:
        input = raw_input
    except NameError:
        pass

    if not force:
        print('Are you sure you want to delete the following resource?')
        print(dumps(data, indent=2, sort_keys=True))

        resp = input('Delete this resource [y/N]? ')
        if resp.upper() != 'Y':
            return

    resource.delete()
    get_logger().info('Resource deleted.')

    if force:
        # If we're running in a script, it's useful to log exactly which
        # resource was deleted (update/create do the same)
        return data


def init(args):
    """Downloads and installs a Stormpath sample project for the given platform."""
    from .main import USER_AGENT

    try:
        auth_args = init_auth(args)
        client = Client(user_agent=USER_AGENT, **auth_args)
    except ValueError as ex:
        get_logger().error(str(ex))
        exit(1)

    type = args.get('<resource>')
    name = args.get('<attributes>')

    if name and len(name) > 0:
        name = name[0].split('name=')[1]

    sample_project = Project.create_from_type(type, name)
    sample_project.download()
    sample_project.create_app(client)
    sample_project.install()


def run(arg):
    """Run a Stormpath sample application."""
    sample_project = Project.detect()
    sample_project.run()


def register(args):
    """Register for Stormpath."""
    data = {}

    try:
        input = raw_input
    except NameError:
        pass

    try:
        if init_auth(args):
            answer = input(colored('It looks like you already have a Stormpath account. Continue anyway? [y/n]: ', 'green'))
            if 'n' in answer:
                exit(1)
    except ValueError:
        pass

    # Register the user on Stormpath.
    done = False
    while not done:
        session = Session()
        resp = session.get('https://api.stormpath.com/register', headers={'accept': 'application/json'})

        data['hpvalue'] = resp.json()['hpvalue']
        data['csrfToken'] = resp.json()['csrfToken']

        print('To register for Stormpath, please enter your information below.\n')
        data['givenName'] = input(colored('First Name: ', 'green'))
        data['surname'] = input(colored('Last Name: ', 'green'))
        data['companyName'] = input(colored('Company Name: ', 'green'))
        data['email'] = input(colored('Email: ', 'green'))
        data['password'] = getpass(colored('Password: ', 'green'))
        data['confirmedPassword'] = getpass(colored('Confirm Password: ', 'green'))

        resp = session.post('https://api.stormpath.com/register', json=data)

        if resp.status_code == 204:
            input(colored('\nSuccessfully created your new Stormpath account!  Please open your email inbox and click the account verification link.  Then come back to this window and press enter.', 'yellow'))
            done = True
        else:
            print(colored('\nERROR: {}\n'.format(resp.json()['message']), 'red'))
            print('Please try again.')

    # Collect the user's tenant name.
    done = False
    while not done:
        tenant = input(colored('\nPlease enter your Stormpath Tenant name (it can be found on the login page in your browser): ', 'green'))
        answer = input(colored('Your Tenant name is: {}, is this correct?  [y\\n]: '.format(tenant), 'green'))

        if 'y' in answer:
            done = True

    # Log the user in.
    done = False
    while not done:
        login_session = Session()
        resp = login_session.get('https://api.stormpath.com/login')

        parser = pq(resp.text)
        csrf_token = parser('input[name="csrfToken"]').val()
        hpvalue = parser('input[name="hpvalue"]').val()

        sleep(3)

        resp = login_session.post('https://api.stormpath.com/login', data={
            'tenantNameKey': tenant,
            'email': data['email'],
            'password': data['password'],
            'csrfToken': csrf_token,
            'hpvalue': hpvalue,
        })

        if resp.status_code != 200:
            print(colored('\nERROR: {}\n'.format(resp.json()['message']), 'red'))
            exit(1)

        done = True

    # Create a new API key pair for this tenant, and download it.
    done = False
    while not done:
        resp = login_session.get('https://api.stormpath.com/v1/accounts/current', headers={'accept': 'application/json'})
        if resp.status_code != 200:
            print(colored('\nERROR: {}\n'.format(resp.json()['message']), 'red'))
            print('Retrying Account request...')

            sleep(1)
            continue

        account_url = resp.json()['href']

        resp = login_session.post(account_url + '/apiKeys', headers={'accept': 'application/json'}, json={'nocache': True})
        if resp.status_code != 201:
            print(colored('\nERROR: {}\n'.format(resp.json()['message']), 'red'))
            print('Retrying API key creation...')

            sleep(1)
            continue

        api_key_url = resp.headers['Location']

        resp = login_session.get(api_key_url, headers={'accept': 'application/json'})
        if resp.status_code != 200:
            print(colored('\nERROR: {}\n'.format(resp.json()['message']), 'red'))
            print('Retrying API key fetching...')

            sleep(1)
            continue

        id = resp.json()['id']
        secret = resp.json()['secret']

        store_config_file('apiKey.properties', 'apiKey.id = {}\napiKey.secret = {}\n'.format(id, secret))
        print(colored('\nSuccessfully created API key for Stormpath usage. Saved as: ~/.stormpath/apiKey.properties', 'yellow'))
        print(colored('You are now setup and ready to use Stormpath!', 'yellow'))

        done = True


def deploy(args):
    """Deploy this Stormpath sample application."""
    project_name = basename(getcwd()) if args['<resource>'] is None else args['<resource>']

    if not which('git'):
        print(colored('\nERROR: It looks like you don\'t have the Git CLI installed, please set this up first.\n', 'red'))
        exit(1)

    if not which('heroku'):
        print(colored('\nERROR: It looks like you don\'t have the Heroku CLI installed, please set this up first.\n', 'red'))
        exit(1)

    try:
        input = raw_input
    except NameError:
        pass

    try:
        answer = input(colored('Attempting to deploy project: {} to Heroku.  Continue? [y/n]: '.format(project_name), 'green'))
        if 'y' not in answer:
            exit(1)
    except ValueError:
        pass

    call(['heroku', 'create', project_name])
    call(['heroku', 'addons:create', 'stormpath'])
    call(['git', 'push', 'heroku', 'master'])

    print(colored('\nYour Stormpath application has been successfully deployed to Heroku! Run `heroku open` to view it in a browser!', 'yellow'))


#: A dictionary of available CLI actions that a user can take.
AVAILABLE_ACTIONS = {
    'list': list_resources,
    'create': create_resource,
    'update': update_resource,
    'delete': delete_resource,
    'set': set_context,
    'context': show_context,
    'setup': setup_credentials,
    'unset': delete_context,
    'status': show_status,
    'init': init,
    'run': run,
    'register': register,
    'deploy': deploy,
}

#: Actions which can be ran locally.
LOCAL_ACTIONS = ('register', 'setup', 'context', 'unset', 'help', 'deploy', 'init', 'run')

#: The default action to use if none is specified.
DEFAULT_ACTION = 'list'

#: The action that sets a value.
SET_ACTION = 'set'

#: The action which provides information about the status of a resource.
STATUS_ACTION = 'status'
