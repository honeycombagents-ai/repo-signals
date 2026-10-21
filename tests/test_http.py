"""HttpClient and UrllibTransport tests. Offline: every transport and opener here is fake."""
import email.message
import io
import os
import traceback
import unittest
import urllib.error
import urllib.request
import urllib.response
from unittest import mock

import _src  # noqa: F401
from repo_signals import http
from repo_signals.http import (MAX_BODY_BYTES, HttpClient, JsonResponse, RawResponse,
                               UrllibTransport, build_opener)
from repo_signals.providers import ProviderError

HOST = "api.example.com"
URL = "https://api.example.com/v2/search"
SENTINELS = ("SENTINELURLPART", "SENTINELHEADER", "SENTINELBODY", "SENTINELQUERY")


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout, max_bytes, method="GET", body=None):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout,
                           "max_bytes": max_bytes, "method": method, "body": body})
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, BaseException):
            raise r
        return r


def ok(body=b'{"ok": true}', status=200, headers=None):
    return RawResponse(status, headers or {}, body)


def everything(e):
    return "".join(traceback.format_exception(e)) + repr(e) + str(e.args)


class LeakFree:
    def assertLeakFree(self, e):
        self.assertIsInstance(e, ProviderError)
        text = everything(e)
        for s in SENTINELS:
            self.assertNotIn(s, text)

    def call_raises(self, client, code, url=URL):
        with self.assertRaises(ProviderError) as cm:
            client.get_json(url, {"q": "SENTINELQUERY"}, {"X-Key": "SENTINELHEADER"}, 5)
        self.assertEqual(cm.exception.code, code)
        self.assertLeakFree(cm.exception)
        return cm.exception


class ClientHappyPath(LeakFree, unittest.TestCase):
    def test_returns_status_and_json(self):
        t = FakeTransport(ok(b'{"a": [1, 2]}', headers={"Retry-After": "3"}))
        r = HttpClient((HOST,), transport=t).get_json(URL, {"domain": "x.example.com",
                                                            "limit": 5}, {"A": "b"}, 7)
        status, data = r
        self.assertEqual((status, data), (200, {"a": [1, 2]}))
        self.assertIsInstance(r, JsonResponse)
        self.assertEqual(r.headers["retry-after"], "3")
        self.assertEqual(t.calls[0]["url"], URL + "?domain=x.example.com&limit=5")
        self.assertEqual(t.calls[0]["headers"], {"A": "b"})
        self.assertEqual(t.calls[0]["timeout"], 7.0)
        self.assertEqual(t.calls[0]["max_bytes"], MAX_BODY_BYTES)
        self.assertEqual(MAX_BODY_BYTES, 1024 * 1024)

    def test_host_match_is_case_insensitive(self):
        t = FakeTransport(ok())
        HttpClient(("API.Example.com",), transport=t).get_json("https://api.EXAMPLE.com/x")
        self.assertEqual(len(t.calls), 1)

    def test_non_2xx_returns_status_and_tolerates_non_json(self):
        for body, data in ((b"<html>SENTINELBODY</html>", None), (b'{"e": 1}', {"e": 1})):
            with self.subTest(body=body):
                t = FakeTransport(ok(body, status=500))
                self.assertEqual(tuple(HttpClient((HOST,), transport=t).get_json(URL)),
                                 (500, data))

    def test_repr_hides_data(self):
        r = JsonResponse(200, {"k": "SENTINELBODY"}, {})
        self.assertNotIn("SENTINEL", repr(r))


class ClientRefusals(LeakFree, unittest.TestCase):
    def test_refuses_http(self):
        t = FakeTransport(ok())
        c = HttpClient((HOST,), transport=t)
        for url in ("http://api.example.com/x", "HTTP://api.example.com/x",
                    "ftp://api.example.com/x", "file:///etc/hosts"):
            with self.subTest(url=url):
                self.call_raises(c, "insecure_scheme", url)
        self.assertEqual(t.calls, [])

    def test_refuses_host_outside_allowlist(self):
        t = FakeTransport(ok())
        c = HttpClient((HOST,), transport=t)
        for url in ("https://evil.example.com/SENTINELURLPART", "https://api.example.com.evil/x",
                    "https://example.com/x", "https://sub.api.example.com/x"):
            with self.subTest(url=url):
                self.call_raises(c, "host_not_allowed", url)
        self.assertEqual(t.calls, [])

    def test_refuses_odd_urls(self):
        t = FakeTransport(ok())
        c = HttpClient((HOST,), transport=t)
        for url in ("https://user" + "@" + "api.example.com/x", "https://api.example.com:8443/x",
                    "https://api.example.com/x?SENTINELURLPART=1", "https://api.example.com/x#f",
                    "https://api.example.com/x y", "https://api.example.com:bad/x", ""):
            with self.subTest(url=url):
                self.call_raises(c, "bad_url", url)
        self.assertEqual(t.calls, [])

    def test_refuses_redirect_without_following(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                t = FakeTransport(ok(b"", status=status,
                                     headers={"Location": "https://api.example.com/next"}))
                self.call_raises(HttpClient((HOST,), transport=t), "redirect_refused")
                self.assertEqual(len(t.calls), 1)

    def test_refuses_oversize_body(self):
        big = b'{"x": "' + b"SENTINELBODY" * 100 + b'"}'
        t = FakeTransport(ok(big))
        self.call_raises(HttpClient((HOST,), transport=t, max_body_bytes=1000),
                         "response_too_large")
        self.assertEqual(t.calls[0]["max_bytes"], 1000)
        t = FakeTransport(ok(b" " * (MAX_BODY_BYTES + 1)))
        self.call_raises(HttpClient((HOST,), transport=t), "response_too_large")
        exact = b'"' + b"a" * (MAX_BODY_BYTES - 2) + b'"'
        t = FakeTransport(ok(exact))
        self.assertEqual(HttpClient((HOST,), transport=t).get_json(URL)[0], 200)

    def test_malformed_json_on_success(self):
        for body in (b"", b"{SENTINELBODY", b"\xff\xfe", b"[" * 100000):
            with self.subTest(n=len(body)):
                self.call_raises(HttpClient((HOST,), transport=FakeTransport(ok(body))),
                                 "malformed_json")

    def test_transport_exceptions_never_leak(self):
        msg = f"failed {URL}?q=SENTINELQUERY SENTINELURLPART SENTINELHEADER SENTINELBODY"
        for exc in (OSError(msg), ValueError(msg), RuntimeError(msg), KeyError(msg)):
            with self.subTest(exc=type(exc).__name__):
                e = self.call_raises(HttpClient((HOST,), transport=FakeTransport(exc)),
                                     "network_error")
                self.assertIsNone(e.__cause__)
                self.assertTrue(e.__suppress_context__)

    def test_transport_provider_errors_pass_through(self):
        e = self.call_raises(HttpClient((HOST,), transport=FakeTransport(ProviderError("timeout"))),
                             "timeout")
        self.assertEqual(str(e), "timeout")

    def test_bad_transport_response(self):
        for r in (None, (200, {}, b"{}"), RawResponse("200", {}, b"{}"),
                  RawResponse(200, {}, "{}")):
            with self.subTest(r=r):
                self.call_raises(HttpClient((HOST,), transport=FakeTransport(r)),
                                 "bad_transport_response")

    def test_bad_headers_and_params(self):
        t = FakeTransport(ok())
        c = HttpClient((HOST,), transport=t)
        for headers in ({"X": "a\r\nSENTINELHEADER: 1"}, {"Bad Name": "v"}, {"X": 1}, ["X"]):
            with self.subTest(headers=headers), self.assertRaises(ProviderError) as cm:
                c.get_json(URL, None, headers, 5)
            self.assertEqual(cm.exception.code, "bad_header")
            self.assertLeakFree(cm.exception)
        for params in ({"q": None}, {"q": True}, {1: "x"}, [("q", "x")]):
            with self.subTest(params=params), self.assertRaises(ProviderError) as cm:
                c.get_json(URL, params, None, 5)
            self.assertEqual(cm.exception.code, "bad_params")
        self.assertEqual(t.calls, [])

    def test_timeout_required_and_bounded(self):
        t = FakeTransport(ok())
        c = HttpClient((HOST,), transport=t)
        for bad in (None, 0, -1, True, float("inf"), float("nan"), 121, "5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                c.get_json(URL, None, None, bad)
        self.assertEqual(t.calls, [])
        c.get_json(URL)
        self.assertEqual(t.calls[0]["timeout"], http.DEFAULT_TIMEOUT)

    def test_constructor_validation(self):
        for hosts in ((), "api.example.com", ("https://api.example.com",), ("a.example.com:443",),
                      ("",), (None,)):
            with self.subTest(hosts=hosts), self.assertRaises(ValueError):
                HttpClient(hosts, transport=FakeTransport(ok()))
        with self.assertRaises(ValueError):
            HttpClient((HOST,), transport=FakeTransport(ok()), proxies={"https": "x"})
        for cap in (0, MAX_BODY_BYTES + 1, True):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                HttpClient((HOST,), transport=FakeTransport(ok()), max_body_bytes=cap)


class PostJson(LeakFree, unittest.TestCase):
    PAYLOAD = {"q": ["SENTINELBODY"], "n": 5}
    HEADERS = {"X-Key": "SENTINELHEADER"}

    def post_raises(self, client, code, url=URL, payload=None):
        with self.assertRaises(ProviderError) as cm:
            client.post_json(url, self.PAYLOAD if payload is None else payload, self.HEADERS, 5)
        self.assertEqual(cm.exception.code, code)
        self.assertLeakFree(cm.exception)
        return cm.exception

    def test_sends_json_body_and_returns_status_and_json(self):
        t = FakeTransport(ok(b'{"people": []}', headers={"Retry-After": "2"}))
        r = HttpClient((HOST,), transport=t).post_json(URL, {"a": [1, "b"]}, {"X": "y"}, 6)
        self.assertEqual(tuple(r), (200, {"people": []}))
        self.assertEqual(r.headers["retry-after"], "2")
        call = t.calls[0]
        self.assertEqual((call["url"], call["method"], call["timeout"]), (URL, "POST", 6.0))
        self.assertEqual(call["body"], b'{"a":[1,"b"]}')
        self.assertEqual(call["headers"], {"X": "y", "Content-Type": "application/json"})
        self.assertEqual(call["max_bytes"], MAX_BODY_BYTES)

    def test_keeps_a_given_content_type(self):
        t = FakeTransport(ok())
        HttpClient((HOST,), transport=t).post_json(URL, {}, {"content-type": "application/json"})
        self.assertEqual(t.calls[0]["headers"], {"content-type": "application/json"})

    def test_get_still_sends_no_body(self):
        t = FakeTransport(ok())
        HttpClient((HOST,), transport=t).get_json(URL)
        self.assertEqual((t.calls[0]["method"], t.calls[0]["body"]), ("GET", None))

    def test_refuses_http(self):
        t = FakeTransport(ok())
        for url in ("http://api.example.com/x", "ftp://api.example.com/x"):
            with self.subTest(url=url):
                self.post_raises(HttpClient((HOST,), transport=t), "insecure_scheme", url)
        self.assertEqual(t.calls, [])

    def test_refuses_host_outside_allowlist(self):
        t = FakeTransport(ok())
        for url in ("https://evil.example.com/SENTINELURLPART", "https://api.example.com.evil/x"):
            with self.subTest(url=url):
                self.post_raises(HttpClient((HOST,), transport=t), "host_not_allowed", url)
        self.assertEqual(t.calls, [])

    def test_refuses_query_in_url(self):
        t = FakeTransport(ok())
        self.post_raises(HttpClient((HOST,), transport=t), "bad_url",
                         URL + "?SENTINELQUERY=1")
        self.assertEqual(t.calls, [])

    def test_refuses_redirect_without_following(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                t = FakeTransport(ok(b"", status=status,
                                     headers={"Location": "https://api.example.com/next"}))
                self.post_raises(HttpClient((HOST,), transport=t), "redirect_refused")
                self.assertEqual(len(t.calls), 1)

    def test_refuses_oversize_body(self):
        t = FakeTransport(ok(b'"' + b"SENTINELBODY" * 100 + b'"'))
        self.post_raises(HttpClient((HOST,), transport=t, max_body_bytes=1000),
                         "response_too_large")
        t = FakeTransport(ok(b" " * (MAX_BODY_BYTES + 1)))
        self.post_raises(HttpClient((HOST,), transport=t), "response_too_large")

    def test_malformed_json_on_success(self):
        for body in (b"", b"{SENTINELBODY", b"\xff"):
            with self.subTest(body=body):
                self.post_raises(HttpClient((HOST,), transport=FakeTransport(ok(body))),
                                 "malformed_json")

    def test_non_2xx_tolerates_non_json(self):
        t = FakeTransport(ok(b"<html>SENTINELBODY</html>", status=503))
        self.assertEqual(tuple(HttpClient((HOST,), transport=t).post_json(URL, {})), (503, None))

    def test_bad_and_oversize_payloads(self):
        t = FakeTransport(ok())
        c = HttpClient((HOST,), transport=t)
        for payload in (["SENTINELBODY"], "SENTINELBODY", {"x": float("nan")},
                        {"x": {"SENTINELBODY"}}, {"x": object()}):
            with self.subTest(payload=type(payload).__name__):
                self.post_raises(c, "bad_payload", payload=payload)
        self.post_raises(c, "payload_too_large",
                         payload={"x": "SENTINELBODY" * (http.MAX_REQUEST_BYTES // 10)})
        self.assertEqual(t.calls, [])

    def test_bad_headers_and_timeout(self):
        t = FakeTransport(ok())
        c = HttpClient((HOST,), transport=t)
        with self.assertRaises(ProviderError) as cm:
            c.post_json(URL, {}, {"X": "a\nSENTINELHEADER"}, 5)
        self.assertEqual(cm.exception.code, "bad_header")
        self.assertLeakFree(cm.exception)
        for bad in (None, 0, 121, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                c.post_json(URL, {}, None, bad)
        self.assertEqual(t.calls, [])

    def test_transport_exceptions_never_leak(self):
        msg = f"failed {URL} SENTINELHEADER SENTINELBODY SENTINELURLPART"
        for exc in (OSError(msg), ValueError(msg), RuntimeError(msg)):
            with self.subTest(exc=type(exc).__name__):
                e = self.post_raises(HttpClient((HOST,), transport=FakeTransport(exc)),
                                     "network_error")
                self.assertIsNone(e.__cause__)
                self.assertTrue(e.__suppress_context__)


class PutJsonAndPostForm(LeakFree, unittest.TestCase):
    def test_put_json(self):
        t = FakeTransport(ok(b'{"updated": 1}'))
        r = HttpClient((HOST,), transport=t).put_json(URL, {"values": [["a"]]}, {"X": "y"}, 6,
                                                       {"opt": "RAW"})
        self.assertEqual(tuple(r), (200, {"updated": 1}))
        call = t.calls[0]
        self.assertEqual((call["method"], call["url"], call["body"]),
                         ("PUT", URL + "?opt=RAW", b'{"values":[["a"]]}'))
        self.assertEqual(call["headers"]["Content-Type"], "application/json")

    def test_post_json_params_go_in_the_query(self):
        t = FakeTransport(ok())
        HttpClient((HOST,), transport=t).post_json(URL, {}, None, 5, {"a": "1", "b": 2})
        self.assertEqual(t.calls[0]["url"], URL + "?a=1&b=2")
        self.call_raises_put(HttpClient((HOST,), transport=t), "bad_params", params={"a": None})

    def test_post_form(self):
        t = FakeTransport(ok(b'{"t": 1}'))
        HttpClient((HOST,), transport=t).post_form(URL, {"grant": "x y", "a": "1&2"})
        call = t.calls[0]
        self.assertEqual((call["method"], call["body"]), ("POST", b"grant=x+y&a=1%262"))
        self.assertEqual(call["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        for bad in ({"a": 1}, ["a"], {1: "a"}):
            with self.subTest(bad=bad), self.assertRaises(ProviderError) as cm:
                HttpClient((HOST,), transport=t).post_form(URL, bad)
            self.assertEqual(cm.exception.code, "bad_payload")
        self.assertEqual(len(t.calls), 1)

    def call_raises_put(self, client, code, url=URL, params=None):
        with self.assertRaises(ProviderError) as cm:
            client.put_json(url, {"q": "SENTINELBODY"}, {"X-Key": "SENTINELHEADER"}, 5, params)
        self.assertEqual(cm.exception.code, code)
        self.assertLeakFree(cm.exception)

    def test_put_refusals(self):
        t = FakeTransport(ok())
        c = HttpClient((HOST,), transport=t)
        self.call_raises_put(c, "insecure_scheme", "http://api.example.com/x")
        self.call_raises_put(c, "host_not_allowed", "https://evil.example.com/SENTINELURLPART")
        self.call_raises_put(c, "bad_url", URL + "?SENTINELQUERY=1")
        with self.assertRaises(ProviderError) as cm:
            c.post_form("https://evil.example.com/x", {"assertion": "SENTINELBODY"})
        self.assertEqual(cm.exception.code, "host_not_allowed")
        self.assertEqual(t.calls, [])
        t = FakeTransport(ok(b"", status=308, headers={"Location": URL}))
        self.call_raises_put(HttpClient((HOST,), transport=t), "redirect_refused")
        self.assertEqual(len(t.calls), 1)
        t = FakeTransport(ok(b'"' + b"SENTINELBODY" * 100 + b'"'))
        self.call_raises_put(HttpClient((HOST,), transport=t, max_body_bytes=500),
                             "response_too_large")

    def test_put_passes_through_urllib(self):
        o = FakeOpener(FakeResp(200, b"{}"))
        UrllibTransport(opener=o)(URL, {}, 4.0, 100, method="PUT", body=b"{}")
        self.assertEqual(o.requests[0][0].get_method(), "PUT")

    def test_no_delete_or_patch_anywhere(self):
        self.assertEqual(http.ALLOWED_METHODS, ("GET", "POST", "PUT"))
        for name in dir(HttpClient):
            for bad in ("delete", "patch", "clear"):
                self.assertNotIn(bad, name.lower())


# ---- UrllibTransport, with a fake opener or a fake HTTPS handler (no sockets) ----

class FakeResp:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = io.BytesIO(body)
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v
        self.read_sizes = []

    def read(self, n=-1):
        self.read_sizes.append(n)
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class TransportTests(LeakFree, unittest.TestCase):
    def test_passes_url_headers_timeout(self):
        o = FakeOpener(FakeResp(200, b'{"a": 1}', {"Retry-After": "2"}))
        r = UrllibTransport(opener=o)(URL + "?q=1", {"X-Key": "k"}, 4.0, 100)
        self.assertEqual(r, RawResponse(200, {"retry-after": "2"}, b'{"a": 1}'))
        req, timeout = o.requests[0]
        self.assertEqual(req.full_url, URL + "?q=1")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("X-key"), "k")
        self.assertEqual(timeout, 4.0)

    def test_reads_at_most_cap_plus_one(self):
        resp = FakeResp(200, b"x" * (3 * MAX_BODY_BYTES))
        r = UrllibTransport(opener=FakeOpener(resp))(URL, {}, 4.0, 1000)
        self.assertEqual(len(r.body), 1001)
        self.assertLessEqual(sum(resp.read_sizes), 1001)

    def test_post_passes_method_and_body(self):
        o = FakeOpener(FakeResp(200, b'{"a": 1}'))
        r = UrllibTransport(opener=o)(URL, {"X-Key": "k"}, 4.0, 100, method="POST",
                                      body=b'{"q":1}')
        self.assertEqual(r.status, 200)
        req, _ = o.requests[0]
        self.assertEqual((req.get_method(), req.data), ("POST", b'{"q":1}'))

    def test_refuses_other_methods_and_bodies(self):
        o = FakeOpener(FakeResp())
        for kw in ({"method": "PATCH"}, {"method": "DELETE"}, {"method": "HEAD"},
                   {"method": "POST", "body": "str"}):
            with self.subTest(kw=kw), self.assertRaises(ProviderError) as cm:
                UrllibTransport(opener=o)(URL, {}, 4.0, 100, **kw)
            self.assertEqual(cm.exception.code, "bad_method")
        self.assertEqual(o.requests, [])

    def test_refuses_http_itself(self):
        o = FakeOpener(FakeResp())
        with self.assertRaises(ProviderError) as cm:
            UrllibTransport(opener=o)("http://api.example.com/x", {}, 4.0, 100)
        self.assertEqual(cm.exception.code, "insecure_scheme")
        self.assertEqual(o.requests, [])

    def test_http_error_becomes_status(self):
        err = urllib.error.HTTPError(URL, 429, "Too Many", email.message.Message(),
                                     io.BytesIO(b'{"errors": []}'))
        err.headers["Retry-After"] = "7"
        r = UrllibTransport(opener=FakeOpener(err))(URL, {}, 4.0, 100)
        self.assertEqual((r.status, r.headers.get("retry-after"), r.body),
                         (429, "7", b'{"errors": []}'))

    def test_errors_map_to_codes_without_leaks(self):
        leak = f"{URL}?q=SENTINELQUERY SENTINELHEADER SENTINELBODY"
        cases = [(TimeoutError(leak), "timeout"),
                 (urllib.error.URLError(TimeoutError(leak)), "timeout"),
                 (urllib.error.URLError(leak), "network_error"),
                 (ConnectionResetError(leak), "network_error"),
                 (__import__("ssl").SSLError(leak), "tls_error"),
                 (urllib.error.URLError(__import__("ssl").SSLError(leak)), "tls_error"),
                 (ValueError(leak), "network_error")]
        for exc, code in cases:
            with self.subTest(exc=type(exc).__name__, code=code):
                with self.assertRaises(ProviderError) as cm:
                    UrllibTransport(opener=FakeOpener(exc))(URL, {"X": "SENTINELHEADER"}, 4.0, 9)
                self.assertEqual(cm.exception.code, code)
                self.assertLeakFree(cm.exception)

    def test_ignores_environment_proxies(self):
        env = {"HTTPS_PROXY": "http://proxy.example.com:1", "https_proxy": "http://p.example.com:1",
               "ALL_PROXY": "http://p.example.com:1"}
        with mock.patch.dict(os.environ, env):
            t = UrllibTransport()
            handlers = t._opener.handlers
        # urllib drops a ProxyHandler({}) that handles nothing; none may carry a proxy.
        self.assertEqual([h.proxies for h in handlers
                          if isinstance(h, urllib.request.ProxyHandler) and h.proxies], [])
        self.assertEqual(t.proxies, {})

    def test_explicit_proxies_are_used(self):
        t = UrllibTransport(proxies={"https": "http://proxy.example.com:3128"})
        proxy_handlers = [h for h in t._opener.handlers
                          if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(proxy_handlers[0].proxies, {"https": "http://proxy.example.com:3128"})
        for bad in ({"ftp": "x"}, {"https": ""}, ["https"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                UrllibTransport(proxies=bad)

    def test_only_the_refusing_redirect_handler(self):
        handlers = UrllibTransport()._opener.handlers
        redirects = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual(len(redirects), 1)
        self.assertIsInstance(redirects[0], http._NoRedirect)
        req = urllib.request.Request(URL)
        self.assertIsNone(redirects[0].redirect_request(
            req, io.BytesIO(), 302, "Found", email.message.Message(), "https://x.example.com/"))

    def test_real_urllib_stack_does_not_follow_redirect(self):
        seen = []

        class FakeHTTPS(urllib.request.HTTPSHandler):
            def https_open(self, req):
                seen.append(req.full_url)
                hdrs = email.message.Message()
                hdrs["Location"] = "https://api.example.com/elsewhere"
                resp = urllib.response.addinfourl(io.BytesIO(b""), hdrs, req.full_url, 302)
                resp.msg = "Found"
                return resp

        t = UrllibTransport(opener=build_opener({}, https_handler=FakeHTTPS()))
        r = t(URL, {}, 4.0, 100)
        self.assertEqual(r.status, 302)
        self.assertEqual(seen, [URL])
        with self.assertRaises(ProviderError) as cm:
            HttpClient((HOST,), transport=t).get_json(URL)
        self.assertEqual(cm.exception.code, "redirect_refused")
        self.assertEqual(seen, [URL, URL])

    def test_real_urllib_stack_does_not_follow_post_redirect(self):
        seen = []

        class FakeHTTPS(urllib.request.HTTPSHandler):
            def https_open(self, req):
                seen.append((req.get_method(), req.full_url, req.data))
                hdrs = email.message.Message()
                hdrs["Location"] = "https://api.example.com/elsewhere"
                resp = urllib.response.addinfourl(io.BytesIO(b""), hdrs, req.full_url, 307)
                resp.msg = "Temporary Redirect"
                return resp

        t = UrllibTransport(opener=build_opener({}, https_handler=FakeHTTPS()))
        with self.assertRaises(ProviderError) as cm:
            HttpClient((HOST,), transport=t).post_json(URL, {"q": 1})
        self.assertEqual(cm.exception.code, "redirect_refused")
        self.assertEqual(seen, [("POST", URL, b'{"q":1}')])


if __name__ == "__main__":
    unittest.main()
