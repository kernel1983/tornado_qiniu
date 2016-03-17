# -*- coding: utf-8 -*-
import platform

import requests
from requests.auth import AuthBase

from qiniu import config
from .auth import RequestsAuth
from . import __version__

from tornado import gen
from tornado import httpclient

try:
    from mimetools import choose_boundary
except ImportError:
    from email.generator import _make_boundary as choose_boundary

from requests.packages.urllib3.fields import RequestField
from requests.packages.urllib3.filepost import encode_multipart_formdata

_sys_info = '{0}; {1}'.format(platform.system(), platform.machine())
_python_ver = platform.python_version()

USER_AGENT = 'QiniuPython/{0} ({1}; ) Python/{2}'.format(__version__, _sys_info, _python_ver)

_session = None
_http = None
_headers = {'User-Agent': USER_AGENT}


def __return_wrapper(resp):
    if resp.status_code != 200:
        return None, ResponseInfo(resp)
    ret = resp.json() if resp.text != '' else {}
    return ret, ResponseInfo(resp)


def _init():
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=config.get_default('connection_pool'), pool_maxsize=config.get_default('connection_pool'),
        max_retries=config.get_default('connection_retries'))
    session.mount('http://', adapter)
    global _session
    _session = session

    global _http
    _http = httpclient.AsyncHTTPClient()

@gen.coroutine
def _post(url, params, files, auth):
    if _http is None:
        _init()
    try:
        form = UploadForm()
        if params:
            [form.add_field(name, value) for name, value in params.items()]
        form.files = files
        body, content_type = form.encode_body()

        headers = _headers
        headers["Content-Type"] = content_type

        r = yield _http.fetch(
            url, method="POST", headers=headers, body=body, connect_timeout=config.get_default('connection_timeout'))

    except Exception as e:
        raise gen.Return((r, None, ResponseInfo(None, e)))

    raise gen.Return((r, ResponseInfo(r)))

def _get(url, params, auth):
    try:
        r = requests.get(
            url, params=params, auth=RequestsAuth(auth) if auth is not None else None,
            timeout=config.get_default('connection_timeout'), headers=_headers)
    except Exception as e:
        return None, ResponseInfo(None, e)
    return __return_wrapper(r)


class _TokenAuth(AuthBase):
    def __init__(self, token):
        self.token = token

    def __call__(self, r):
        r.headers['Authorization'] = 'UpToken {0}'.format(self.token)
        return r


def _post_with_token(url, data, token):
    return _post(url, data, None, _TokenAuth(token))


def _post_file(url, data, files):
    return _post(url, data, files, None)


def _post_with_auth(url, data, auth):
    return _post(url, data, None, RequestsAuth(auth))


class ResponseInfo(object):
    """七牛HTTP请求返回信息类

    该类主要是用于获取和解析对七牛发起各种请求后的响应包的header和body。

    Attributes:
        status_code: 整数变量，响应状态码
        text_body:   字符串变量，响应的body
        req_id:      字符串变量，七牛HTTP扩展字段，参考 http://developer.qiniu.com/docs/v6/api/reference/extended-headers.html
        x_log:       字符串变量，七牛HTTP扩展字段，参考 http://developer.qiniu.com/docs/v6/api/reference/extended-headers.html
        error:       字符串变量，响应的错误内容
    """

    def __init__(self, response, exception=None):
        """用响应包和异常信息初始化ResponseInfo类"""
        self.__response = response
        self.exception = exception
        if response is None:
            self.status_code = -1
            self.text_body = None
            self.req_id = None
            self.x_log = None
            self.error = str(exception)
        else:
            self.status_code = response.code
            self.text_body = response.body
            self.req_id = response.headers.get('X-Reqid')
            self.x_log = response.headers.get('X-Log')
            if self.status_code >= 400:
                ret = response.json() if response.text != '' else None
                if ret is None or ret['error'] is None:
                    self.error = 'unknown'
                else:
                    self.error = ret['error']

    def ok(self):
        return self.status_code == 200

    def need_retry(self):
        if self.__response is None:
            return True
        code = self.status_code
        if (code // 100 == 5 and code != 579) or code == 996:
            return True
        return False

    def connect_failed(self):
        return self.__response is None

    def __str__(self):
        return ', '.join(['%s:%s' % item for item in self.__dict__.items()])

    def __repr__(self):
        return self.__str__()


class UploadForm(object):
    def __init__(self):
        self.form_fields = []
        self.files = {}
        self.boundary = choose_boundary()
        self.content_type = 'multipart/form-data'
        return

    def get_content_type(self):
        return self.content_type

    def add_field(self, name, value):
        self.form_fields.append((str(name), str(value)))
        return

    def encode_body(self):
        new_fields = []
        for field, val in self.form_fields:
            if isinstance(val, basestring) or not hasattr(val, '__iter__'):
                val = [val]
            for v in val:
                if v is not None:
                    # Don't call str() on bytestrings: in Py3 it all goes wrong.
                    if not isinstance(v, bytes):
                        v = str(v)

                    new_fields.append(
                        (field.decode('utf-8') if isinstance(field, bytes) else field,
                         v.encode('utf-8') if isinstance(v, str) else v))
        if not self.files:
            self.files = {}
        for k, v in self.files.items():
            # support for explicit filename
            ft = None
            fh = None
            if isinstance(v, (tuple, list)):
                if len(v) == 2:
                    fn, fp = v
                elif len(v) == 3:
                    fn, fp, ft = v
                else:
                    fn, fp, ft, fh = v
            else:
                fn = guess_filename(v) or k
                fp = v

            if isinstance(fp, (str, bytes, bytearray)):
                fdata = fp
            else:
                fdata = fp.read()

            rf = RequestField(name=k, data=fdata,
                              filename=fn, headers=fh)
            rf.make_multipart(content_type=ft)
            new_fields.append(rf)

        body, content_type = encode_multipart_formdata(new_fields)
        return body,content_type
