try:
    import ldap
    import ldap.filter
    import ldapurl
except ImportError: # pragma: no cover
    # this is for benefit of being able to build the docs on rtd.org
    class ldap(object):
        LDAPError = Exception
        SCOPE_ONELEVEL = None
        SCOPE_SUBTREE = None
        
import logging
import pprint
import time

from pyramid.exceptions import ConfigurationError
from pyramid.compat import bytes_

try:
    from ldappool import ConnectionManager, BackendError
except ImportError as e: # pragma: no cover
    class ConnectionManager(object):
        def __init__(self, *arg, **kw):
            # this is for benefit of being able to build the docs on rtd.org
            raise e

logger = logging.getLogger(__name__)

class _LDAPQuery(object):
    """ Represents an LDAP query.  Provides rudimentary in-RAM caching of
    query results."""
    def __init__(self, base_dn, filter_tmpl, scope, cache_period,
                  search_after_bind=False):
        self.base_dn = base_dn
        self.filter_tmpl = filter_tmpl
        self.scope = scope
        self.cache_period = cache_period
        self.last_timeslice = 0
        self.cache = {}
        self.search_after_bind = search_after_bind

    def __str__(self):
        return ('base_dn=%(base_dn)s, filter_tmpl=%(filter_tmpl)s, '
                'scope=%(scope)s, cache_period=%(cache_period)s '
                'search_after_bind=%(search_after_bind)s'%
                self.__dict__)

    def query_cache(self, cache_key):
        result = None
        now = time.time()
        ts = _timeslice(self.cache_period, now)

        if ts > self.last_timeslice:
            logger.debug('dumping cache; now ts: %r, last_ts: %r' % (
                ts, 
                self.last_timeslice)
                )
            self.cache = {}
            self.last_timeslice = ts

        result = self.cache.get(cache_key)

        return result

    def execute_cache(self, conn, *cache_key, **kw):
        sizelimit = kw.get('sizelimit', 0)
        logger.debug('searching for %r' % (cache_key,))

        if self.cache_period:
            result = self.query_cache(cache_key)
            if result is not None:
                logger.debug('result for %r retrieved from cache' % 
                             (cache_key,)
                             )
            else:
                result = conn.search_ext_s(*cache_key, sizelimit=sizelimit)
                self.cache[cache_key] = result
        else:
            result = conn.search_ext_s(*cache_key, sizelimit=sizelimit)

        logger.debug('search result: %r' % (result,))

        return result

    def execute(self, conn, sizelimit=0, **kw):
        """ Returns an entry set resulting from querying the connected backend
        for entries matching parameters in kw.

        Skip the real query and return an hard-coded result based on string
        interpolation of ``base_dn`` if the ``filter_tmpl`` attribute
        is empty"""
        search_filter = self.filter_tmpl % kw
        search_base = self.base_dn % kw
        if search_filter:
            cache_key = (
                bytes_(search_base, 'utf-8'),
                self.scope,
                bytes_(search_filter, 'utf-8')
                )
            return self.execute_cache(conn, *cache_key, sizelimit=sizelimit)

        result = [(search_base, {})]
        logger.debug('result generated by string interpolation: %r' % result)
        return result

def _timeslice(period, when=None):
    if when is None: # pragma: no cover
        when =  time.time()
    return when - (when % period)
    
def _activity_identifier(base_identifier, realm=''):
    if realm:
        return '-'.join((base_identifier, realm))
    else:
        return base_identifier

def _registry_identifier(base_identifier, realm=''):
    if realm:
        return '_'.join((base_identifier, realm))
    else:
        return base_identifier

class Connector(object):
    """ Provides API methods for accessing LDAP authentication information."""
    def __init__(self, registry, manager, realm=''):
        self.registry = registry
        self.manager = manager
        self.realm = realm
        self.login_qry_identif = _registry_identifier('ldap_login_query', realm)
        self.group_qry_identif = _registry_identifier('ldap_groups_query', realm)

    def authenticate(self, login='', password=''):
        """ Given a login name and a password, return a tuple of ``(dn,
        attrdict)`` if the matching user if the user exists and his password
        is correct.  Otherwise return ``None``.

        In a ``(dn, attrdict)`` return value, ``dn`` will be the
        distinguished name of the authenticated user.  Attrdict will be a
        dictionary mapping LDAP user attributes to sequences of values.  The
        keys and values in the dictionary values provided will be decoded
        from UTF-8, recursively, where possible.  The dictionary returned is
        a case-insensitive dictionary implementation.

        A zero length password will always be considered invalid since it
        results in a request for "unauthenticated authentication" which should
        not be used for LDAP based authentication. See `section 5.1.2 of
        RFC-4513 <http://tools.ietf.org/html/rfc4513#section-5.1.2>`_ for a
        description of this behavior.

        If :meth:`pyramid.config.Configurator.ldap_set_login_query` was not
        called, using this function will raise an
        :exc:`pyramid.exceptions.ConfiguratorError`."""
        if password == '':
            return None
            
        search = getattr(self.registry, self.login_qry_identif, None)
        if search is None:
            raise ConfigurationError(
                'ldap_set_login_query was not called during setup')
        
        try:
            if login:
                escaped_login = ldap.filter.escape_filter_chars(login)
            else:
                escaped_login = ''
            with self.manager.connection() as conn:
                result = search.execute(conn, login=escaped_login, password=password, sizelimit=1)
                if len(result) == 1:
                    login_dn = result[0][0]
                else:
                    return None
            with self.manager.connection(login_dn, password) as conn:
                # must invoke the __enter__ of this thing for it to connect
                if search.search_after_bind:
                    result = search.execute_cache(conn, login_dn,
                                                   ldap.SCOPE_BASE,
                                                   '(objectClass=*)')
                return _ldap_tag_dn_decode(result, realm=self.realm)[0]
        except (ldap.LDAPError, ldap.SIZELIMIT_EXCEEDED, ldap.INVALID_CREDENTIALS):
            logger.debug('Exception in authenticate with login %r - - ' % login,
                         exc_info=True)
            return None
        except BackendError:
            logger.debug('Exception in authenticate with login %r' % login,
                         exc_info=True)
            return None

    def user_groups(self, userdn):
        """ Given a user DN, return a sequence of LDAP attribute dictionaries
        matching the groups of which the DN is a member.  If the DN does not
        exist, return ``None``.

        In a return value ``[(dn, attrdict), ...]``, ``dn`` will be the
        distinguished name of the group.  Attrdict will be a dictionary
        mapping LDAP group attributes to sequences of values.  The keys and
        values in the dictionary values provided will be decoded from UTF-8,
        recursively, where possible.  The dictionary returned is a
        case-insensitive dictionary implemenation.
        
        If :meth:`pyramid.config.Configurator.ldap_set_groups_query` was not
        called, using this function will raise an
        :exc:`pyramid.exceptions.ConfiguratorError`
        """
        search = getattr(self.registry, self.group_qry_identif, None)
        if search is None:
            raise ConfigurationError(
                'set_ldap_groups_query was not called during setup')
        with self.manager.connection() as conn:
            try:
                result = search.execute(conn, userdn=userdn)
                return _ldap_tag_dn_decode(result)
            except ldap.LDAPError:
                logger.debug(
                    'Exception in user_groups with userdn %r' % userdn,
                    exc_info=True)
                return None

def ldap_set_login_query(config, base_dn, filter_tmpl, 
                          scope=ldap.SCOPE_ONELEVEL, cache_period=0,
                          search_after_bind=False, realm=''):
    """ Configurator method to set the LDAP login search.

    - **base_dn**: the DN at which to begin the search **[mandatory]**
    - **filter_tmpl**: an LDAP search filter **[mandatory]**

    At least one of these parameters should contain the replacement value
    ``%(login)s``

    - **scope**: A valid ldap search scope
      **default**: ``ldap.SCOPE_ONELEVEL``
    - **cache_period**: the number of seconds to cache login search results
      if 0, results will not be cached
      **default**: ``0``
    - **search_after_bind**: do a base search on the entry itself after
      a successful bind

    Example::

        config.set_ldap_login_query(
            base_dn='CN=Users,DC=example,DC=com',
            filter_tmpl='(sAMAccountName=%(login)s)',
            scope=ldap.SCOPE_ONELEVEL,
            )

    The registered search must return one and only one value to be considered
    a valid login.

    If the ``filter_tmpl`` is empty, the directory will not be searched, and
    the entry dn will be assumed to be equal to the ``%(login)s``-replaced
    ``base_dn``, and no entry's attribute will be fetched from the LDAP server,
    leading to faster operation.
    Both in this case, and in the case of servers configured to only allow
    reading some needed entry's attribute only to the bound entry itself,
    ``search_after_bind`` can be set to ``True`` if there is a need to read
    the entry's attribute.

    Example::

        config.set_ldap_login_query(
            base_dn='sAMAccountName=%(login)s,CN=Users,DC=example,DC=com',
            filter_tmpl=''
            scope=ldap.SCOPE_ONELEVEL,
            search_after_bind=True
            )

    """
    query_identif = _registry_identifier('ldap_login_query', realm)
    intr_identif = _registry_identifier('pyramid_ldap', realm)
    act_identif = _activity_identifier('pyramid_ldap', realm)

    query = _LDAPQuery(base_dn, filter_tmpl, scope, cache_period,
                        search_after_bind=search_after_bind)
    def register():
        setattr(config.registry, query_identif, query)

    intr = config.introspectable(
        '%s login query' % intr_identif,
        None,
        str(query),
        'login query'
        )
        
    config.action(act_identif, register, introspectables=(intr,))

def ldap_set_groups_query(config, base_dn, filter_tmpl, 
                           scope=ldap.SCOPE_SUBTREE, cache_period=0, realm=''):
    """ Configurator method to set the LDAP groups search.

    - **base_dn**: the DN at which to begin the search **[mandatory]**
    - **filter_tmpl**: a string which can be used as an LDAP filter:
      it should contain the replacement value ``%(userdn)s`` **[mandatory]**
    - **scope**: A valid ldap search scope
      **default**: ``ldap.SCOPE_SUBTREE``
    - **cache_period**: the number of seconds to cache login search results
      if 0, results will not be cached
      **default**: ``0``

    Example::

        config.set_ldap_groups_query(
            base_dn='CN=Users,DC=example,DC=com',
            filter_tmpl='(&(objectCategory=group)(member=%(userdn)s))'
            scope=ldap.SCOPE_SUBTREE,
            )

    """
    query_identif = _registry_identifier('ldap_groups_query', realm)
    intr_identif = _registry_identifier('pyramid_ldap', realm)
    act_identif = _activity_identifier('ldap-set-groups-query', realm)

    query = _LDAPQuery(base_dn, filter_tmpl, scope, cache_period)

    def register():
        setattr(config.registry, query_identif, query)

    intr = config.introspectable(
        '%s groups query' % intr_identif,
        None,
        str(query),
        '%s groups query' % intr_identif
        )

    config.action(act_identif, register, introspectables=(intr,))

def ldap_setup(config, uri, bind=None, passwd=None, pool_size=10, retry_max=3,
               retry_delay=.1, use_tls=False, timeout=-1, use_pool=True, realm=''):
    """ Configurator method to set up an LDAP connection pool.

    - **uri**: ldap server uri **[mandatory]**
    - **bind**: default bind that will be used to bind a connector.
      **default: None**
    - **passwd**: default password that will be used to bind a connector.
      **default: None**
    - **size**: pool size. **default: 10**
    - **retry_max**: number of attempts when a server is down. **default: 3**
    - **retry_delay**: delay in seconds before a retry. **default: .1**
    - **use_tls**: activate TLS when connecting. **default: False**
    - **timeout**: connector timeout. **default: -1**
    - **use_pool**: activates the pool. If False, will recreate a connector
       each time. **default: True**
    """
    conn_identif = _registry_identifier('ldap_connector', realm)
    intr_identif = _registry_identifier('pyramid_ldap', realm)
    act_identif = _activity_identifier('ldap-setup', realm)

    vals = dict(
        uri=uri, bind=bind, passwd=passwd, size=pool_size, 
        retry_max=retry_max, retry_delay=retry_delay, use_tls=use_tls, 
        timeout=timeout, use_pool=use_pool
        )

    manager = ConnectionManager(**vals)

    def get_connector(request):
        registry = request.registry
        return Connector(registry, manager, realm)

    config.set_request_property(get_connector, conn_identif, reify=True)

    intr = config.introspectable(
        '%s setup' % intr_identif,
        None,
        pprint.pformat(vals),
        '%s setup' % intr_identif,
        )
    config.action(act_identif, None, introspectables=(intr,))

def get_ldap_connector_name(realm=''):
    """ Return the name of the connector attached to the request
    for the named **realm**."""
    return _registry_identifier('ldap_connector', realm)

def get_ldap_connector(request, realm=''):
    """ Return the LDAP connector attached to the request.  If
    :meth:`pyramid.config.Configurator.ldap_setup` was not called, using
    this function will raise an :exc:`pyramid.exceptions.ConfigurationError`."""
    conn_name = get_ldap_connector_name(realm)
    connector = getattr(request, conn_name, None)
    if connector is None:
        raise ConfigurationError(
            'You must call Configurator.ldap_setup during setup '
            'to use an ldap connector')
    return connector

def groupfinder(userdn, request):
    """ A groupfinder implementation useful in conjunction with
    out-of-the-box Pyramid authentication policies.  It returns the DN of
    each group belonging to the user specified by ``userdn`` to as a
    principal in the list of results; if the user does not exist, it returns
    None."""
    parsed = ldapurl.LDAPUrl(userdn)
    connector = get_ldap_connector(request, realm=parsed.hostport)
    group_list = connector.user_groups(parsed.dn)
    if group_list is None:
        return None
    group_dns = []
    for dn, attrs in group_list:
        group_dns.append(dn)
    return group_dns

def _ldap_decode(result):
    """ Decode (recursively) strings in the result data structure to Unicode
    using the utf-8 encoding """
    return _Decoder().decode(result)

def _ldap_tag_dn_decode(result, realm=''):

    return [('ldap://%s/%s' % (realm, ldapurl.ldapUrlEscape(dn)),
                                                               attributes)
                   for dn, attributes in _ldap_decode(result)
            ]

class _Decoder(object):
    """
    Stolen from django-auth-ldap.
    
    Encodes and decodes strings in a nested structure of lists, tuples, and
    dicts. This is helpful when interacting with the Unicode-unaware
    python-ldap.
    """

    ldap = ldap

    def __init__(self, encoding='utf-8'):
        self.encoding = encoding

    def decode(self, value):
        try:
            if isinstance(value, str):
                value = value.decode(self.encoding)
            elif isinstance(value, list):
                value = self._decode_list(value)
            elif isinstance(value, tuple):
                value = tuple(self._decode_list(value))
            elif isinstance(value, dict):
                value = self._decode_dict(value)
        except UnicodeDecodeError:
            pass

        return value

    def _decode_list(self, value):
        return [self.decode(v) for v in value]

    def _decode_dict(self, value):
        # Attribute dictionaries should be case-insensitive. python-ldap
        # defines this, although for some reason, it doesn't appear to use it
        # for search results.
        decoded = self.ldap.cidict.cidict()

        for k, v in value.iteritems():
            decoded[self.decode(k)] = self.decode(v)

        return decoded

def includeme(config):
    """ Set up Configurator methods for pyramid_ldap """
    config.add_directive('ldap_setup', ldap_setup)
    config.add_directive('ldap_set_login_query', ldap_set_login_query)
    config.add_directive('ldap_set_groups_query', ldap_set_groups_query)

