# -*- coding: utf-8 -*-
"""
    werkzeug.contrib.sessions
    ~~~~~~~~~~~~~~~~~~~~~~~~~

    This module contains some helper classes that helps one to add session
    support to a python WSGI application.

    Example::

        from werkzeug.contrib.sessions import SessionMiddleware, \
             FilesystemSessionStore

        app = SessionMiddleware(app, FilesystemSessionStore())

    The current session will then appear in the WSGI environment as
    `werkzeug.session`.  However it's recommended to not use the middleware
    but the stores directly in the application.  However for very simple
    scripts a middleware for sessions could be sufficient.

    This module does not implement methods or ways to check if a session is
    expired.  That should be done by a cronjob and storage specific.  For
    example to prune unused filesystem sessions one could check the modified
    time of the files.  It sessions are stored in the database the new()
    method should add an expiration timestamp for the session.


    :copyright: 2007 by Armin Ronacher.
    :license: BSD, see LICENSE for more details.
"""
import re
from os import path, unlink
from time import time
from random import Random
try:
    from hashlib import sha1
except ImportError:
    from sha import new as sha1
from cPickle import dump, load, HIGHEST_PROTOCOL
from Cookie import SimpleCookie, Morsel
from werkzeug.utils import ClosingIterator


_sha1_re = re.compile(r'^[a-fA-F0-9]{40}$')


class Session(dict):
    """
    Subclass of a dict that keeps track of direct object changes.  Changes
    in mutable structures are not tracked, for those you have to set
    `modified` to `True` by hand.
    """

    __slots__ = ('modified', 'sid', 'new')

    def __init__(self, data, sid, new=False):
        dict.__init__(self, data)
        self.modified = False
        self.sid = sid
        self.new = new

    def __repr__(self):
        return '<%s %s%s>' % (
            self.__class__.__name__,
            dict.__repr__(self),
            self.should_save and '*' or ''
        )

    def should_save(self):
        """True if the session should be saved."""
        return self.modified or self.new
    should_save = property(should_save)

    def copy(self):
        """Create a flat copy of the session."""
        result = self.__class__(dict(self), self.sid, self.new)
        result.modified = self.modified
        return result

    def call_with_modification(f):
        def oncall(self, *args, **kw):
            try:
                return f(self, *args, **kw)
            finally:
                self.modified = True
        try:
            oncall.__name__ = f.__name__
            oncall.__doc__ = f.__doc__
            oncall.__module__ = f.__module__
        except:
            pass
        return oncall

    __setitem__ = call_with_modification(dict.__setitem__)
    __delitem__ = call_with_modification(dict.__delitem__)
    clear = call_with_modification(dict.clear)
    pop = call_with_modification(dict.pop)
    popitem = call_with_modification(dict.popitem)
    setdefault = call_with_modification(dict.setdefault)
    update = call_with_modification(dict.update)
    del call_with_modification


class SessionStore(object):
    """
    Baseclass for all session stores.  The Werkzeug contrib module does not
    implement any useful stores beside the filesystem store, application
    developers are encouraged to create their own stores.
    """

    def __init__(self, session_class=None):
        if session_class is None:
            session_class = Session
        self.session_class = session_class

    def is_valid_key(self, key):
        """Check if a key has the correct format."""
        return _sha1_re.match(key) is not None

    def generate_key(self, salt=None):
        """Simple function that generates a new session key."""
        return sha1('%s|%s|%s' % (Random(salt).random(), time(),
                                  salt)).hexdigest()

    def new(self):
        """Generate a new session."""
        return self.session_class({}, self.generate_key(), True)

    def save(self, session):
        """Save a session."""

    def save_if_modified(self, session):
        """Save if a session class wants an update."""
        if session.should_save:
            self.save(session)

    def delete(self, session):
        """Delete a session."""

    def get(self, sid):
        """
        Get a session for this sid or a new session object.  This method has
        to check if the session key is valid and create a new session if it
        that wasn't the case.
        """
        return self.session_class({}, sid, True)


class FilesystemSessionStore(SessionStore):
    """
    Simple example session store that saves session on the filesystem like
    PHP does.
    """

    def __init__(self, path=None, filename_template='werkzeug_%s.sess',
                 session_class=Session):
        SessionStore.__init__(self, session_class)
        if path is None:
            from tempfile import gettempdir
            path = gettempdir()
        self.path = path
        self.filename_template = filename_template

    def get_session_filename(self, sid):
        return path.join(self.path, self.filename_template % sid)

    def save(self, session):
        f = file(self.get_session_filename(session.sid), 'wb')
        try:
            dump(dict(session), f, HIGHEST_PROTOCOL)
        finally:
            f.close()

    def delete(self, session):
        fn = self.get_session_filename(session.sid)
        try:
            unlink(fn)
        except OSError:
            pass

    def get(self, sid):
        fn = self.get_session_filename(sid)
        if not self.is_valid_key(sid) or not path.exists(fn):
            return self.new()
        else:
            f = file(fn, 'rb')
            try:
                data = load(f)
            finally:
                f.close()
        return self.session_class(data, sid, False)


class SessionMiddleware(object):
    """
    A simple middleware that puts the session object of a store provided into
    the WSGI environ.  It automatically sets cookies and restores sessions.

    However a middleware is not the preferred solution because it won't be as
    fast as sessions managed by the application itself.
    """

    def __init__(self, app, store, cookie_name='session_id',
                 cookie_age=None, cookie_path=None, cookie_domain=None,
                 cookie_secure=None, environ_key='werkzeug.session'):
        self.app = app
        self.store = store
        self.cookie_name = cookie_name
        self.cookie_age = cookie_age
        self.cookie_path = cookie_path
        self.cookie_domain = cookie_domain
        self.cookie_secure = cookie_secure
        self.environ_key = environ_key

    def __call__(self, environ, start_response):
        cookie = SimpleCookie(environ.get('HTTP_COOKIE', ''))
        morsel = cookie.get(self.cookie_name, None)
        if morsel is None:
            session = self.store.new()
        else:
            session = self.store.get(morsel.value)
        environ[self.environ_key] = session

        def injecting_start_response(status, headers, exc_info=None):
            if session.should_save:
                morsel = Morsel()
                morsel.key = self.cookie_name
                morsel.coded_value = session.sid
                if self.cookie_age is not None:
                    morsel['max-age'] = self.cookie_age
                    morsel['expires'] = cookie_date(time() + self.cookie_age)
                if self.cookie_domain is not None:
                    morsel['domain'] = self.cookie_domain
                if self.cookie_path is not None:
                    morsel['path'] = self.cookie_path
                if self.cookie_secure is not None:
                    morsel['secure'] = self.cookie_secure
                headers.append(tuple(str(morsel).split(':', 1)))
            return start_response(status, headers, exc_info)
        return ClosingIterator(self.app(environ, injecting_start_response),
                               lambda: self.store.save_if_modified(session))
