import unittest

from data_sources import ResilientHttpClient, SourceUnavailable


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.content = text.encode()
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ResilientHttpClientTests(unittest.TestCase):
    def make_client(self, responses):
        sleeps = []
        client = ResilientHttpClient(
            session=FakeSession(responses), retries=2, eastmoney_min_interval=0.01,
            sleeper=sleeps.append, randomizer=lambda _a, _b: 0,
        )
        return client, sleeps

    def test_429_retries_then_succeeds(self):
        client, sleeps = self.make_client([FakeResponse(429), FakeResponse(200, {"ok": True})])
        result = client.get_json("https://push2.eastmoney.com/example")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(client.session.calls), 2)
        self.assertTrue(sleeps)

    def test_403_opens_circuit_without_retry(self):
        client, _ = self.make_client([FakeResponse(403)])
        with self.assertRaises(SourceUnavailable):
            client.get_json("https://push2.eastmoney.com/example")
        with self.assertRaises(SourceUnavailable):
            client.get_json("https://push2.eastmoney.com/another")
        self.assertEqual(len(client.session.calls), 1)
        self.assertEqual(client.trace()[0]["status"], "circuit_open")

    def test_fallback_records_route(self):
        client, _ = self.make_client([])
        result, route = client.call_with_fallback(
            lambda: (_ for _ in ()).throw(RuntimeError("primary down")),
            lambda: {"ok": True},
            "测试数据",
        )
        self.assertEqual(route, "fallback")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.trace()[-1]["status"], "fallback_success")

    def test_three_consecutive_failures_open_host_circuit(self):
        client, _ = self.make_client([FakeResponse(500), FakeResponse(500), FakeResponse(500)])
        client.retries = 0
        for index in range(3):
            with self.assertRaises(SourceUnavailable):
                client.get_json(f"https://example.com/{index}")
        with self.assertRaises(SourceUnavailable):
            client.get_json("https://example.com/blocked")
        self.assertEqual(len(client.session.calls), 3)
        self.assertEqual(client.trace()[-1]["status"], "circuit_open")

    def test_eastmoney_remote_disconnect_retries_once_then_opens_shared_circuit(self):
        import requests

        client, _ = self.make_client([
            requests.ConnectionError("RemoteDisconnected: Remote end closed connection"),
            requests.ConnectionError("RemoteDisconnected: Remote end closed connection"),
        ])
        with self.assertRaises(SourceUnavailable):
            client.get_json("https://push2.eastmoney.com/example")
        # datacenter shares the same upstream circuit, so no further network call.
        with self.assertRaises(SourceUnavailable):
            client.get_json("https://datacenter-web.eastmoney.com/api/data/v1/get")
        self.assertEqual(len(client.session.calls), 2)
        self.assertEqual(client.trace()[-1]["status"], "circuit_open")


if __name__ == "__main__":
    unittest.main()
