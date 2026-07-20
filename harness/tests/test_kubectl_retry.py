"""Тесты _kubectl — обёртки над kubectl с повтором транзиентных сбоев.

Регрессия 20.07 (серия final): одиночный ненулевой возврат `kubectl get job`
на опросе условий убивал жертву целиком — вместе с её строкой результата и
метрикой решения placement_regret. Причина сбоя при этом не восстанавливалась:
stderr оставался в capture_output и нигде не печатался. Здесь проверяем оба
свойства — повтор и внятное сообщение.

Запуск из harness/:

    .venv/bin/python -m unittest discover tests
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from submit import k8s_submit


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestKubectlRetry(unittest.TestCase):
    def test_first_attempt_ok(self):
        with mock.patch.object(
            k8s_submit.subprocess, "run", return_value=_completed(0, "Complete=True ")
        ) as run:
            result = k8s_submit._kubectl(["kubectl", "get", "job"], what="опрос")
        self.assertEqual(result.stdout, "Complete=True ")
        self.assertEqual(run.call_count, 1)

    def test_transient_failure_then_success(self):
        """Ровно тот случай, что стоил нам жертвы: моргнул API, следующая
        попытка проходит — данные не теряются."""
        attempts = [
            _completed(1, stderr="Unable to connect to the server: i/o timeout"),
            _completed(0, "Complete=True "),
        ]
        with mock.patch.object(
            k8s_submit.subprocess, "run", side_effect=attempts
        ) as run, mock.patch.object(k8s_submit.time, "sleep"):
            result = k8s_submit._kubectl(["kubectl", "get", "job"], what="опрос")
        self.assertEqual(result.stdout, "Complete=True ")
        self.assertEqual(run.call_count, 2)

    def test_persistent_failure_reports_stderr(self):
        """Постоянный сбой всё-таки падает — но с причиной в тексте, иначе
        разбирать постфактум нечего."""
        failure = _completed(1, stderr='jobs.batch "x" not found')
        with mock.patch.object(
            k8s_submit.subprocess, "run", return_value=failure
        ) as run, mock.patch.object(k8s_submit.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                k8s_submit._kubectl(["kubectl", "get", "job"], what="опрос условий")
        self.assertEqual(run.call_count, k8s_submit.KUBECTL_RETRIES)
        self.assertIn("not found", str(ctx.exception))
        self.assertIn("опрос условий", str(ctx.exception))

    def test_no_check_true_left_on_wait_path(self):
        """check=True на пути ожидания/сбора возвращает прежнее поведение —
        молчаливую потерю точки. Тест держит эту дверь закрытой."""
        source = Path(k8s_submit.__file__).read_text(encoding="utf-8")
        wait_path = source[source.index("def wait_for_completion"):source.index("def record_result")]
        self.assertNotIn("check=True", wait_path)


if __name__ == "__main__":
    unittest.main()
