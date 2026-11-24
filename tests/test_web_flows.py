# -*- coding: utf-8 -*-
"""End-to-end HTTP flows: upload, view, withdraw, download."""
import csv
import io


def _upload(client, text):
    return client.post("/upload", data={"csv_text": text}, follow_redirects=True)


def test_health_and_empty_dashboard(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
    page = client.get("/")
    assert page.status_code == 200
    assert "供数总览".encode() in page.data


def test_upload_view_download_withdraw_recover(client):
    batch1 = (
        "图版号,题名,引用号\n"
        "A1,图版A1,A2;A3\n"
        "A2,图版A2,\n"
        "A3,图版A3,A4\n"
        "B1,独立B1,B2\n"
        "B2,独立B2,\n"
    )
    page = _upload(client, batch1)
    assert page.status_code == 200
    body = page.data.decode()
    assert "暂缓供数" in body and "A1" in body

    state = client.get("/api/state").get_json()
    assert set(x["number"] for x in state["serving"]) == {"A2", "B1", "B2"}
    assert set(x["number"] for x in state["suspended"]) == {"A1", "A3"}
    assert [m["number"] for m in state["missing"]] == ["A4"]
    # Incremental scope covered all five changed seeds' component.
    assert set(state["latest_change"]["changed"]) == {"A1", "A2", "A3", "B1", "B2"}

    # Download must be exactly the active published index.
    dl = client.get("/download.csv")
    assert dl.status_code == 200
    assert "attachment" in dl.headers["Content-Disposition"]
    text = dl.data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["图版号", "题名", "引用号"]
    downloaded_numbers = {r[0] for r in rows[1:]}
    api_index = client.get("/api/index").get_json()
    assert downloaded_numbers == {e["number"] for e in api_index["entries"]}
    assert downloaded_numbers == {"A2", "B1", "B2"}

    # 补录 A4 -> A3/A1 auto recover.
    _upload(client, "图版号,题名,引用号\nA4,补录A4,A2\n")
    state = client.get("/api/state").get_json()
    assert state["stats"]["serving"] == 6
    assert state["stats"]["suspended"] == 0

    # Withdraw A3 through the web form -> A1 suspended again.
    resp = client.post("/withdraw", data={"number": "A3"}, follow_redirects=True)
    assert resp.status_code == 200
    state = client.get("/api/state").get_json()
    assert set(x["number"] for x in state["suspended"]) == {"A1"}
    assert {m["number"] for m in state["missing"]} == {"A3"}
    downloaded_numbers = {
        r[0] for r in csv.reader(io.StringIO(client.get("/download.csv").data.decode("utf-8-sig")))
        if r and r[0] != "图版号"
    }
    assert "A1" not in downloaded_numbers and "A3" not in downloaded_numbers
    assert downloaded_numbers == {"A2", "A4", "B1", "B2"}

    # Refill via multipart file upload.
    data = {
        "csv_file": (io.BytesIO("图版号,题名,引用号\nA3,图版A3,A4\n".encode()), "fill.csv"),
    }
    resp = client.post("/upload", data=data, content_type="multipart/form-data",
                       follow_redirects=True)
    assert resp.status_code == 200
    state = client.get("/api/state").get_json()
    assert state["stats"]["suspended"] == 0
    # 5 initial + A4 + refilled A3 = 6 plates.
    assert state["stats"]["serving"] == 6


def test_invalid_csv_rejected_and_index_unchanged(client):
    _upload(client, "图版号,题名,引用号\nA1,好题,\n")
    idx_before = client.get("/api/index").get_json()["version"]

    resp = _upload(client, "图版号,题名,引用号\n,缺号,B\n")
    assert resp.status_code == 422
    assert "整批未入库".encode() in resp.data

    idx_after = client.get("/api/index").get_json()["version"]
    assert idx_after == idx_before
    state = client.get("/api/state").get_json()
    assert state["stats"]["total"] == 1


def test_withdraw_multiple_numbers_textarea(client):
    _upload(client, "图版号,题名,引用号\nA,题A,\nB,题B,\nC,题C,A\n")
    resp = client.post(
        "/withdraw", data={"numbers": "A\nB"}, follow_redirects=True
    )
    assert resp.status_code == 200
    state = resp  # response unused below
    data = client.get("/api/state").get_json()
    # A withdrawn; C references A -> suspended; B withdrawn.
    assert data["stats"]["total"] == 1
    assert [x["number"] for x in data["suspended"]] == ["C"]
