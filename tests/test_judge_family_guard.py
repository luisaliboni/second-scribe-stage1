"""F1a item 5: for a real corpus the judge may not be Qwen-family or the reconstructor's own model."""
import unittest

from tests.addr import ip
from second_scribe.llm import HTTPClient, ModelConfig, RealGuardRefused, guard_real_clients

OFF = {"chat_template_kwargs": {"enable_thinking": False}}


def http(model, name, ip):
    return HTTPClient(ModelConfig(name=name, endpoint=f"http://{ip}:8000/v1", model_id=model, params=dict(OFF)))


def no_dns(host, port):
    raise AssertionError("no lookup expected")


class TestJudgeFamily(unittest.TestCase):
    def check(self, recon, judge, judge2=None):
        clients = {"recon": http(recon, "recon", f"{ip(10,0,0,1)}"), "judge": http(judge, "judge", f"{ip(10,0,0,2)}")}
        if judge2:
            clients["judge2"] = http(judge2, "judge2", f"{ip(10,0,0,3)}")
        guard_real_clients(clients, resolver=no_dns)

    def test_a_non_qwen_judge_passes(self):
        self.check("mtplx-qwen38-flashnext", "deepseek-v4-flash-0731")
        self.check("mtplx-qwen38-flashnext", "gemma-4-31b", "deepseek-v4-flash-0731")

    def test_a_qwen_family_judge_is_refused(self):
        for judge in ("Qwen3-32B", "mtplx-qwen38-flashnext", "org/QWEN2.5-72B-Instruct", "qwq-32b", "my-qwen.gguf"):
            with self.assertRaises(RealGuardRefused, msg=judge) as cm:
                self.check("deepseek-v4-flash-0731", judge)
            self.assertIn("judge", str(cm.exception))
            self.assertIn("Qwen", str(cm.exception))

    def test_a_qwen_second_judge_is_refused(self):
        with self.assertRaises(RealGuardRefused) as cm:
            self.check("qwen-a", "deepseek-j", "qwen3-32b")
        self.assertIn("judge2", str(cm.exception))

    def test_a_judge_that_is_the_reconstructor_is_refused(self):
        for recon, judge in (("deepseek-j", "deepseek-j"), ("deepseek-j", "DeepSeek-J"), ("org/model-x", "model-x")):
            with self.assertRaises(RealGuardRefused, msg=(recon, judge)) as cm:
                self.check(recon, judge)
            self.assertIn("reconstructor", str(cm.exception))

    def test_a_qwen_reconstructor_is_the_normal_case(self):
        self.check("mtplx-qwen38-flashnext", "gemma-4-31b")


if __name__ == "__main__":
    unittest.main()
