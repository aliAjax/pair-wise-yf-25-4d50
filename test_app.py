import tempfile
import threading
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

    def _reviewed_paper(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        return paper_id

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        self.store.submit_rebuttal("alice", paper_id, "感谢意见，我们将补充对比并解释实验结论。")
        result = self.store.decide("chair", paper_id, "minor_revision", "补充实验后接收。")
        self.assertEqual(result["decision"], "minor_revision")
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.assertIsNotNone(self.store.get_paper("chair", paper_id)["author_id"])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        self.assertGreaterEqual(len(history), 8)

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("alice", paper_id, "r2")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_paper("r2", paper_id)
        self.assertEqual(ctx.exception.status, 403)

    def test_withdraw_cancels_pending_and_keeps_completed(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]  # invited，未处理
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r2", a2, True)  # accepted，待评审
        a3 = self.store.assign("chair", paper_id, "r3")["id"]
        self.store.respond_assignment("r3", a3, True)
        self.store.submit_review("r3", a3, 5, "工作非常扎实，建议直接接收。")  # completed
        result = self.store.withdraw("alice", paper_id, "作者发现实验数据有误")
        self.assertEqual(result["status"], "withdrawn")
        self.assertEqual(result["cancelled_assignments"], 2)
        self.assertEqual(self.store.get_paper("chair", paper_id)["status"], "withdrawn")
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT reviewer_id,status,score FROM assignments WHERE paper_id=? ORDER BY reviewer_id", (paper_id,)
            ).fetchall()
        statuses = {row["reviewer_id"]: row["status"] for row in rows}
        self.assertEqual(statuses, {"r1": "cancelled", "r2": "cancelled", "r3": "completed"})
        # 已完成评审与撤稿时间线保留给主席。
        self.assertEqual([row["score"] for row in rows if row["reviewer_id"] == "r3"], [5])
        actions = [item["action"] for item in self.store.history("chair", paper_id)]
        self.assertIn("review.submit", actions)
        self.assertEqual(actions[-1], "paper.withdraw")

    def test_withdraw_releases_reviewer_load(self):
        paper_ids = [self._paper() for _ in range(3)]
        for pid in paper_ids:
            self.store.assign("chair", pid, "r1")  # r1 负载上限为 3，已满载
        extra = self._paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", extra, "r1")
        self.assertEqual(ctx.exception.code, "reviewer_at_capacity")
        self.store.withdraw("alice", paper_ids[0], "不再需要评审")
        self.store.assign("chair", extra, "r1")  # 负载已释放，可再次分配

    def test_withdraw_requires_reason_author_and_open_paper(self):
        paper_id = self._paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw("alice", paper_id, "   ")
        self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw("r1", paper_id, "非作者尝试撤稿")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw("bob", paper_id, "他人论文")
        self.assertEqual(ctx.exception.status, 404)
        self.store.withdraw("alice", paper_id, "主动撤稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw("alice", paper_id, "重复撤稿")
        self.assertEqual(ctx.exception.status, 409)

    def test_withdraw_and_decide_are_mutually_exclusive(self):
        paper_id = self._reviewed_paper()
        self.store.withdraw("alice", paper_id, "作者主动撤稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair", paper_id, "accept")
        self.assertEqual(ctx.exception.status, 409)
        other = self._reviewed_paper()
        self.store.decide("chair", other, "accept")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw("alice", other, "决定后撤稿")
        self.assertEqual(ctx.exception.status, 409)

    def test_concurrent_withdraw_and_decide_only_one_succeeds(self):
        paper_id = self._reviewed_paper()
        results, errors = [], []
        barrier = threading.Barrier(2)

        def run(fn):
            try:
                barrier.wait()
                results.append(fn())
            except BusinessError as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(lambda: self.store.withdraw("alice", paper_id, "并发撤稿"),)),
            threading.Thread(target=run, args=(lambda: self.store.decide("chair", paper_id, "accept"),)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status, 409)
        final = self.store.get_paper("chair", paper_id)["status"]
        self.assertIn(final, {"withdrawn", "decided"})

    def test_chair_filters_papers_by_status(self):
        keep = self._paper()
        gone = self._paper()
        self.store.withdraw("alice", gone, "重复投稿")
        withdrawn = self.store.list_papers("chair", status="withdrawn")
        self.assertEqual([p["id"] for p in withdrawn], [gone])
        submitted = self.store.list_papers("chair", status="submitted")
        self.assertEqual([p["id"] for p in submitted], [keep])
        self.assertEqual(len(self.store.list_papers("chair")), 2)
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_papers("chair", status="bogus")
        self.assertEqual(ctx.exception.status, 422)


if __name__ == "__main__":
    unittest.main()
