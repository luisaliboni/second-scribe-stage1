"""F1a item 1: a real corpus may only be sent to a loopback, private-range or Tailscale endpoint."""
import tempfile
import unittest
from pathlib import Path

from tests.addr import ip
from second_scribe.audit.registry import AuditLog
from second_scribe.llm import (EgressRefused, HTTPClient, MockClient, ModelConfig, endpoint_host, endpoint_problem,
                               guard_real_clients, log_call)

NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}


def no_dns(host, port):
    raise AssertionError(f"unexpected name lookup for {host}")


def fake_dns(table):
    def resolve(host, port):
        return table[host]
    return resolve


def client(url, model="m-1", params=NO_THINK, name="recon"):
    return HTTPClient(ModelConfig(name=name, endpoint=url, model_id=model, params=dict(params)))


class TestEndpointProblem(unittest.TestCase):
    def test_public_hosts_and_addresses_are_refused(self):
        for url in ("https://openrouter.ai/api/v1", "https://api.openai.com/v1", f"http://{ip(8,8,8,8)}:8000/v1",
                    f"http://{ip(203,0,113,5)}/v1", "http://[2001:db8::1]:8000/v1", f"http://{ip(100,63,255,255)}/v1",
                    f"http://{ip(100,128,0,1)}/v1", f"http://{ip(172,32,0,1)}/v1", f"http://{ip(172,15,0,1)}/v1", f"http://{ip(11,0,0,1)}/v1",
                    f"http://{ip(169,254,1,1)}/v1", f"http://{ip(192,169,0,1)}/v1", f"http://{ip(0,0,0,0)}/v1"):
            self.assertIsNotNone(endpoint_problem(url, resolver=lambda h, p: [f"{ip(93,184,216,34)}"]), url)

    def test_loopback_private_and_tailscale_literals_are_accepted(self):
        for url in ("http://127.0.0.1:8000/v1", "http://127.9.9.9/v1", "http://[::1]:8000/v1", f"http://{ip(10,1,2,3)}/v1",
                    f"http://{ip(172,16,0,1)}/v1", f"http://{ip(172,31,255,254)}/v1", f"http://{ip(192,168,1,20)}:8001/v1",
                    f"http://{ip(100,99,1,2)}:8000/v1", f"http://{ip(100,64,0,1)}/v1", f"http://{ip(100,127,255,255)}/v1"):
            self.assertIsNone(endpoint_problem(url, resolver=no_dns), url)

    def test_an_ipv4_mapped_ipv6_address_is_judged_by_its_ipv4_part(self):
        self.assertIsNone(endpoint_problem(f"http://[::ffff:{ip(10,0,0,1)}]/v1", resolver=no_dns))
        self.assertIsNotNone(endpoint_problem(f"http://[::ffff:{ip(8,8,8,8)}]/v1", resolver=no_dns))

    def test_the_host_is_the_real_host_not_text_that_looks_like_one(self):
        # userinfo, a private-looking subdomain and a path that names a private address do not make a public host private
        res = lambda h, p: [f"{ip(93,184,216,34)}"]  # noqa: E731
        for url in ("http://127.0.0.1@evil.example/v1", f"http://{ip(10,0,0,1)}.evil.example/v1",
                    "https://evil.example/127.0.0.1/v1", f"https://evil.example/v1?host={ip(192,168,0,1)}",
                    "https://localhost.evil.example/v1"):
            self.assertIsNotNone(endpoint_problem(url, resolver=res), url)

    def test_a_name_must_resolve_and_every_address_must_be_allowed(self):
        self.assertIsNone(endpoint_problem("http://studio.tailnet.example:8000/v1",
                                           resolver=fake_dns({"studio.tailnet.example": [f"{ip(100,99,1,2)}"]})))
        self.assertIsNone(endpoint_problem("http://localhost:8000/v1",
                                           resolver=fake_dns({"localhost": ["127.0.0.1", "::1"]})))
        # one public address among private ones is enough to refuse
        self.assertIsNotNone(endpoint_problem("http://mixed.example/v1",
                                              resolver=fake_dns({"mixed.example": [f"{ip(10,0,0,1)}", f"{ip(93,184,216,34)}"]})))
        self.assertIsNotNone(endpoint_problem("http://unresolvable.example/v1", resolver=fake_dns({"unresolvable.example": []})))

    def test_a_failed_lookup_refuses(self):
        def broken(host, port):
            raise OSError("no dns")
        self.assertIsNotNone(endpoint_problem("http://studio.example/v1", resolver=broken))

    def test_odd_numeric_hosts_are_judged_by_what_they_resolve_to(self):
        # 2130706433 is 127.0.0.1 as a decimal number; a resolver that follows inet_aton says so, and one that does
        # not yields nothing: either way a public address cannot slip through.
        self.assertIsNone(endpoint_problem("http://2130706433/v1", resolver=fake_dns({"2130706433": ["127.0.0.1"]})))
        self.assertIsNotNone(endpoint_problem("http://2130706433/v1", resolver=fake_dns({"2130706433": [f"{ip(8,8,8,8)}"]})))

    def test_other_schemes_and_malformed_urls_are_refused(self):
        for url in ("ftp://127.0.0.1/v1", "file:///etc/passwd", "127.0.0.1:8000/v1", "", "http:///v1",
                    "http://127.0.0.1:notaport/v1"):
            self.assertIsNotNone(endpoint_problem(url, resolver=no_dns), url)

    def test_endpoint_host_has_no_scheme_path_userinfo_or_query(self):
        self.assertEqual(endpoint_host("https://user:pw@Studio.Example:8443/v1/x?token=abc"), "studio.example:8443")
        self.assertEqual(endpoint_host(f"http://{ip(10,0,0,5)}/v1"), f"{ip(10,0,0,5)}")
        self.assertEqual(endpoint_host("http://[::1]:8000/v1"), "::1:8000")
        self.assertIsNone(endpoint_host(""))


class TestGuardRealClients(unittest.TestCase):
    def test_a_public_endpoint_on_any_role_is_refused(self):
        for role in ("recon", "judge", "judge2"):
            clients = {"recon": client("http://127.0.0.1:8000/v1", "qwen-x"),
                       "judge": client(f"http://{ip(10,0,0,2)}:8001/v1", "deepseek-j", name="judge"),
                       "judge2": client(f"http://{ip(192,168,0,3)}:8002/v1", "gemma-j", name="judge2")}
            clients[role] = client("https://openrouter.ai/api/v1", "deepseek-j" if role != "recon" else "m",
                                   name=role)
            with self.assertRaises(EgressRefused, msg=role) as cm:
                guard_real_clients(clients, resolver=lambda h, p: [f"{ip(104,18,0,1)}"])
            self.assertIn(role, str(cm.exception))
            self.assertIn("openrouter.ai", str(cm.exception))

    def test_private_endpoints_pass(self):
        guard_real_clients({"recon": client("http://127.0.0.1:8000/v1", "qwen-x"),
                            "judge": client(f"http://{ip(10,0,0,2)}:8001/v1", "deepseek-j", name="judge"),
                            "judge2": client(f"http://{ip(100,99,1,2)}:8002/v1", "gemma-j", name="judge2")}, resolver=no_dns)

    def test_offline_clients_have_no_endpoint_and_pass(self):
        guard_real_clients({"recon": MockClient(fn=lambda m: "x"), "judge": MockClient(fn=lambda m: "x")}, resolver=no_dns)
        guard_real_clients({}, resolver=no_dns)

    def test_the_refusal_comes_before_any_request(self):
        sent = []
        c = client("https://api.openai.com/v1")
        c._request = lambda body: sent.append(body)     # would be called by complete()
        with self.assertRaises(EgressRefused):
            guard_real_clients({"recon": c}, resolver=lambda h, p: [f"{ip(104,18,0,1)}"])
        self.assertEqual(sent, [])


class TestHostIsLogged(unittest.TestCase):
    def test_each_call_request_names_the_endpoint_host_but_not_the_url_or_key(self):
        d = tempfile.mkdtemp()
        cfg = ModelConfig(name="recon", endpoint=f"http://user:SECRETPW@{ip(10,1,2,3)}:8000/v1/path?token=abc", model_id="m",
                          api_key_env="SS_TEST_KEY")
        mock = MockClient(fn=lambda m: "ok")
        mock.cfg = cfg
        log = AuditLog(Path(d) / "r.jsonl", "r")
        _text, recs = log_call(log, mock, [{"role": "user", "content": "x"}], stage="recon", item_id="i",
                               temperature=0, seed=1, max_tokens=5)
        req = recs[0]["payload"]
        self.assertEqual(req["endpoint_host"], f"{ip(10,1,2,3)}:8000")
        blob = Path(d, "r.jsonl").read_text()
        for leaked in ("SECRETPW", "token=abc", "/v1/path", "http://"):
            self.assertNotIn(leaked, blob)

    def test_a_client_without_a_config_records_no_host(self):
        d = tempfile.mkdtemp()
        log = AuditLog(Path(d) / "r.jsonl", "r")
        _t, recs = log_call(log, MockClient(fn=lambda m: "ok"), [{"role": "user", "content": "x"}], stage="s",
                            item_id="i", temperature=0, seed=1, max_tokens=5)
        self.assertNotIn("endpoint_host", recs[0]["payload"])


if __name__ == "__main__":
    unittest.main()
