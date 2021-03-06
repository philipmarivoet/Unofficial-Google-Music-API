#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Definitions shared by multiple clients."""

import logging
import sys

from google.protobuf.descriptor import FieldDescriptor

import gmusicapi
from gmusicapi.compat import json
from gmusicapi.exceptions import (
    CallFailure, ParseException, ValidationException,
)
from gmusicapi.utils import utils

log = logging.getLogger(__name__)

#There's a lot of code here to simplify call definition, but it's not scary - promise.
#Request objects are currently requests.Request; see http://docs.python-requests.org


class BuildRequestMeta(type):
    """Metaclass to create build_request from static/dynamic config."""

    def __new__(cls, name, bases, dct):
        #To not mess with mro and inheritance, build the class first.
        new_cls = super(BuildRequestMeta, cls).__new__(cls, name, bases, dct)

        merge_keys = ('headers', 'params')
        all_keys = ('method', 'url', 'files', 'data', 'verify') + merge_keys

        config = {}  # stores key: val for static or f(*args, **kwargs) -> val for dyn
        dyn = lambda key: 'dynamic_' + key
        stat = lambda key: 'static_' + key
        has_key = lambda key: hasattr(new_cls, key)
        get_key = lambda key: getattr(new_cls, key)

        for key in all_keys:
            if not has_key(dyn(key)) and not has_key(stat(key)):
                continue  # this key will be ignored; requests will default it

            if has_key(dyn(key)):
                config[key] = get_key(dyn(key))
            else:
                config[key] = get_key(stat(key))

        for key in merge_keys:
            #merge case: dyn took precedence above, but stat also exists
            if dyn(key) in config and has_key(stat(key)):
                def key_closure(stat_val=get_key(stat(key)), dyn_func=get_key(dyn(key))):
                    def build_key(*args, **kwargs):
                        dyn_val = dyn_func(*args, **kwargs)

                        stat_val.update(dyn_val)
                        return stat_val
                    return build_key
                config[key] = key_closure()

        #To explain some of the funkiness wrt closures, see:
        # http://stackoverflow.com/questions/233673/lexical-closures-in-python

        #create the actual build_request method
        def req_closure(config=config):
            def build_request(cls, *args, **kwargs):
                req_kwargs = {}
                for key, val in config.items():
                    if hasattr(val, '__call__'):
                        val = val(*args, **kwargs)

                    req_kwargs[key] = val

                return req_kwargs
                #return Request(**req_kwargs)
            return build_request

        new_cls.build_request = classmethod(req_closure())

        return new_cls


class Call(object):
    """
    Clients should use Call.perform().

    Calls define how to build their requests through static and dynamic data.
    For example, a request might always send some user-agent: this is static.
    Or, it might need the name of a song to modify: this is dynamic.

    Specially named fields define the data, and correspond with requests.Request kwargs:
        method: eg 'GET' or 'POST'
        url: string
        files: dictionary of {filename: fileobject} files to multipart upload.
        data: the body of the request
                If a dictionary is provided, form-encoding will take place.
                A string will be sent as-is.
        verify: if True, verify SSL certs
        params (m): dictionary of URL parameters to append to the URL.
        headers (m): dictionary

    Static data shold prepends static_ to a field:
        class SomeCall(Call):
            static_url = 'http://foo.com/thiscall'

    And dynamic data prepends dynamic_ to a method:
        class SomeCall(Call):
            #*args, **kwargs are passed from SomeCall.build_request (and Call.perform)
            def dynamic_url(endpoint):
                return 'http://foo.com/' + endpoint

    Dynamic data takes precedence over static if both exist,
     except for attributes marked with (m) above. These get merged, with dynamic overriding
     on key conflicts (though all this really shouldn't be relied on).
     
    Here's a contrived example that merges static and dynamic headers:
        class SomeCall(Call):
            static_headers = {'user-agent': "I'm totally a Google client!"}

            @classmethod
            def dynamic_headers(cls, keep_alive=False):
                return {'Connection': keep_alive}

    If neither a static nor dynamic member is defined, the param is not used to create the requests.Request.


    There's also three static bool fields to declare what auth the session should send:
        send_xt: xsrf token in param/cookie 

     AND/OR

        send_clientlogin: google clientlogin cookies
     OR
        send_sso: google SSO (authtoken) cookies

    Calls must define parse_response.
    Calls can also define filter_response, validate and check_success.

    Calls are organized semantically, so one endpoint might have multiple calls.
    """

    __metaclass__ = BuildRequestMeta

    gets_logged = True

    send_xt = False
    send_clientlogin = False
    send_sso = False

    @classmethod
    def parse_response(cls, response):
        """Parses a requests.Response to data."""
        raise NotImplementedError

    @classmethod
    def validate(cls, response, msg):
        """Raise ValidationException on problems.
        
        :param response: a requests.Response
        :param msg: the result of parse_response on response
        """
        pass

    @classmethod
    def check_success(cls, response, msg):
        """Raise CallFailure on problems.
                
        :param response: a requests.Response
        :param msg: the result of parse_response on response
        """
        pass

    @classmethod
    def filter_response(cls, msg):
        """Return a version of a parsed response appropriate for logging."""
        return msg  # default to identity

    @classmethod
    def get_auth(cls):
        """Return a 3-tuple send_(xt, clientlogin, sso)."""
        return (cls.send_xt, cls.send_clientlogin, cls.send_sso)

    @classmethod
    def perform(cls, session, *args, **kwargs):
        """Send, parse, validate and check success of this call.
        *args and **kwargs are passed to protocol.build_transaction.

        :param session: a PlaySession used to send this request.
        """
        #TODO link up these docs

        call_name = cls.__name__

        if cls.gets_logged:
            log.debug("%s(args=%s, kwargs=%s)",
                      call_name,
                      [utils.truncate(a) for a in args],
                      dict((k, utils.truncate(v)) for (k, v) in kwargs.items())
                      )
        else:
            log.debug("%s(<does not get logged>)", call_name)

        req_kwargs = cls.build_request(*args, **kwargs)

        response = session.send(req_kwargs, cls.get_auth())

        #TODO check return code

        try:
            msg = cls.parse_response(response)
        except ParseException:
            if cls.gets_logged:
                log.exception("couldn't parse %s response: %r", call_name, response.content)
            raise CallFailure("the server's response could not be understood."
                              " The call may still have succeeded, but it's unlikely.",
                              call_name)

        if cls.gets_logged:
            log.debug(cls.filter_response(msg))

        try:
            #order is important; validate only has a schema for a successful response
            cls.check_success(response, msg)
            cls.validate(response, msg)
        except CallFailure:
            raise
        except ValidationException:
            #TODO link to some protocol for reporting this and trim the response if it's huge
            if cls.gets_logged:
                log.exception(
                    "please report the following unknown response format for %s: %r",
                    call_name, msg
                )

        return msg

    @staticmethod
    def _parse_json(text):
        try:
            return json.loads(text)
        except ValueError as e:
            trace = sys.exc_info()[2]
            raise ParseException(str(e)), None, trace

    @staticmethod
    def _filter_proto(msg, make_copy=True):
        """Filter all byte fields in the message and submessages."""
        filtered = msg
        if make_copy:
            filtered = msg.__class__()
            filtered.CopyFrom(msg)

        fields = filtered.ListFields()

        #eg of filtering a specific field
        #if any(fd.name == 'field_name' for fd, val in fields):
        #    filtered.field_name = '<name>'

        #Filter all byte fields.
        for field_name, val in ((fd.name, val) for fd, val in fields
                                if fd.type == FieldDescriptor.TYPE_BYTES):
            setattr(filtered, field_name, "<%s bytes>" % len(val))

        #Filter submessages.
        for field in (val for fd, val in fields
                      if fd.type == FieldDescriptor.TYPE_MESSAGE):

            #protobuf repeated api is bad for reflection
            is_repeated = hasattr(field, '__len__')

            if not is_repeated:
                Call._filter_proto(field, make_copy=False)

            else:
                for i in range(len(field)):
                    #repeatedComposite does not allow setting
                    old_fields = [f for f in field]
                    del field[:]

                    field.extend([Call._filter_proto(f, make_copy=False)
                                  for f in old_fields])

        return filtered


class ClientLogin(Call):
    """Performs `Google ClientLogin
    <https://developers.google.com/accounts/docs/AuthForInstalledApps#ClientLogin>`__."""

    gets_logged = False

    static_method = 'POST'
    #static_headers = {'User-agent': 'Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1'}
    static_url = 'https://www.google.com/accounts/ClientLogin'

    @classmethod
    def dynamic_data(cls, Email, Passwd, accountType='HOSTED_OR_GOOGLE',
                     service='sj', source=None,
                     logintoken=None, logincaptcha=None):
        """Params align with those in the actual request.

        If *source* is ``None``, ``'gmusicapi-<version>'`` is used.

        Captcha requests are not yet implemented.
        """
        if logintoken is not None or logincaptcha is not None:
            raise ValueError('ClientLogin captcha handling is not yet implemented.')

        if source is None:
            source = 'gmusicapi-' + gmusicapi.__version__

        return dict(
            (name, val) for (name, val) in locals().items()
            if name in set(('Email', 'Passwd', 'accountType', 'service', 'source',
                            'logintoken', 'logincaptcha'))
        )

    @classmethod
    def parse_response(cls, response):
        """Return a dictionary of response key/vals.

        A successful login will have SID, LSID, and Auth keys.
        """

        # responses are formatted as, eg:
        #    SID=DQAAAGgA...7Zg8CTN
        #    LSID=DQAAAGsA...lk8BBbG
        #    Auth=DQAAAGgA...dk3fA5N
        # or:
        #    Url=http://www.google.com/login/captcha
        #    Error=CaptchaRequired
        #    CaptchaToken=DQAAAGgA...dkI1LK9
        #    CaptchaUrl=Captcha?ctoken=HiteT...

        ret = {}
        for line in response.text.split('\n'):
            if '=' in line:
                var, val = line.split('=', 1)
                ret[var] = val

        return ret

    @classmethod
    def check_succes(cls, response, msg):
        if response.status_code == 200:
            raise CallFailure("status code %s != 200" % response.status_code, cls.__name__)
