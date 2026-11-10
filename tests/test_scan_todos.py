from __future__ import annotations

import pytest

from ebook_server.core.models import ScanPage, ScanTodo
from tests.test_scan_jobs import _create_scan_job, _png_for, _put_scan_page, _upload_scan_page


@pytest.mark.django_db(transaction=True)
def test_request_rescan_creates_todo() -> None:
    client, job = _create_scan_job("scanner-rescan-create")

    upload_response = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"page-one",
        filename="page1.png",
    )
    assert upload_response.status_code == 201

    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    response = client.post(
        f"/scan/pages/{page.id}/rescan/",
        data={"reason": "bleed-through"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["created"] is True
    assert payload["todo"]["state"] == ScanTodo.State.OPEN
    assert ScanTodo.objects.filter(scan_page=page, state=ScanTodo.State.OPEN).count() == 1


@pytest.mark.django_db(transaction=True)
def test_rescan_workflow_claim_and_complete() -> None:
    client, job = _create_scan_job("scanner-rescan-workflow")

    upload_response = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"page-todo",
        filename="page.png",
    )
    assert upload_response.status_code == 201
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    todo_response = client.post(
        f"/scan/pages/{page.id}/rescan/",
        data={"reason": "blurred edge"},
    )
    assert todo_response.status_code == 201
    todo_id = todo_response.json()["todo"]["id"]

    claim_response = client.post(f"/scan/todos/{todo_id}/claim/")
    assert claim_response.status_code == 200
    assert claim_response.json()["state"] == ScanTodo.State.CLAIMED

    complete_response = client.post(f"/scan/todos/{todo_id}/complete/")
    assert complete_response.status_code == 200
    assert complete_response.json()["state"] == ScanTodo.State.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_scan_jobs_with_open_todos_filter() -> None:
    client_with_todo, job_with_todo = _create_scan_job("scanner-open-todo")
    client_without_todo, job_without_todo = _create_scan_job("scanner-no-todo")

    upload_response = _upload_scan_page(
        client_with_todo,
        str(job_with_todo.id),
        sequence=1,
        side="single",
        payload=b"open-todo-page",
        filename="todo.png",
    )
    assert upload_response.status_code == 201
    page = ScanPage.objects.get(
        scan_job=job_with_todo,
        logical_sequence_number=1,
        side="single",
    )

    todo_response = client_with_todo.post(f"/scan/pages/{page.id}/rescan/")
    assert todo_response.status_code == 201

    response = client_with_todo.get("/scan/jobs/?with_open_todos=true")
    assert response.status_code == 200
    payload = response.json()
    items = payload["items"]
    assert any(item["id"] == str(job_with_todo.id) for item in items)
    assert all(item["id"] != str(job_without_todo.id) for item in items)
    for item in items:
        if item["id"] == str(job_with_todo.id):
            assert item["open_rescan_todo_count"] == 1
            break
    else:
        raise AssertionError("expected job with todo not found")

    response = client_with_todo.get(f"/scan/jobs/{job_with_todo.id}/todos/")
    assert response.status_code == 200
    todo_payload = response.json()
    assert todo_payload["scan_job_id"] == str(job_with_todo.id)
    assert todo_payload["count"] == 1
    assert todo_payload["items"][0]["state"] == ScanTodo.State.OPEN


@pytest.mark.django_db(transaction=True)
def test_replacement_upload_completes_open_rescan_todos() -> None:
    client, job = _create_scan_job("scanner-rescan-replace")
    upload_response = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"page-before",
        filename="before.png",
    )
    assert upload_response.status_code == 201
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    todo_response = client.post(
        f"/scan/pages/{page.id}/rescan/",
        data={"reason": "needs better exposure"},
    )
    assert todo_response.status_code == 201

    replace_response = _put_scan_page(client, page.id, _png_for(b"replacement"))
    assert replace_response.status_code == 201

    todo_completed = ScanTodo.objects.filter(scan_page=page).values_list("state", flat=True)
    assert ScanTodo.State.COMPLETED in set(todo_completed)
    assert ScanTodo.objects.filter(scan_page=page, state=ScanTodo.State.OPEN).count() == 0
    assert ScanTodo.objects.filter(scan_page=page, state=ScanTodo.State.COMPLETED).count() >= 1
