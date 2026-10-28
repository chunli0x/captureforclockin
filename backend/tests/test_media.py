"""Robot vision video safety, private ingest, delivery and accounting regressions."""
import hashlib
import json
import struct
from unittest.mock import Mock
import pytest
from sqlalchemy import select, func
from seeker.db import Actor, IngestJob, Judgment, Job, MediaAccess, MediaAsset, MediaVariant, uid
from seeker.media import (MediaError, face_clear, inspection_clear, inspect_video, dhash,
                          perceptual_duplicate, strip_metadata, SIGHTENGINE_VIDEO_ENDPOINT)
from seeker.media_ingest import asset_path, bucket, sandbox, transcode, write_private
from seeker.media_worker import MediaWorker
from test_judgment import pilot, pilot_database_url, run_work, seed_pair


def clean_inspection():
    return {"status": "success", "data": {"frames": [
        {"info": {"position": i * 500}, "faces": [], "artificial_faces": [],
         "text": {"has_artificial": 0.01, "has_natural": 0.01, "boxes": []}}
        for i in range(8)]}}


@pytest.mark.parametrize("frame", [None, {}, {"faces": None}, {"faces": False},
                                    {"faces": {}}, {"faces": [{}]}, {"faces": "[]"}])
def test_faces_fail_closed(frame):
    assert not face_clear(frame)


def test_moderation_requires_nested_complete_face_and_text_coverage():
    assert face_clear({"faces": [], "artificial_faces": []})
    assert not face_clear({"faces": []})
    assert inspection_clear(clean_inspection())
    assert not inspection_clear({"status": "success", "frames": clean_inspection()["data"]["frames"]})
    for field in ("faces", "artificial_faces", "text", "info"):
        payload = clean_inspection()
        del payload["data"]["frames"][3][field]
        assert not inspection_clear(payload)
    for value in (None, float('nan'), -1, True, 0.8):
        payload = clean_inspection()
        payload["data"]["frames"][2]["text"]["has_natural"] = value
        assert not inspection_clear(payload)
    seconds_instead_of_ms = clean_inspection()
    for frame in seconds_instead_of_ms['data']['frames']:
        frame['info']['position'] /= 1000
    assert not inspection_clear(seconds_instead_of_ms)
    for bad in ([], clean_inspection()["data"]["frames"][:2]):
        assert not inspection_clear({"status": "success", "data": {"frames": bad}})


def test_sightengine_allowlisted_endpoint_bytes_and_no_redirects(tmp_path, monkeypatch):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.post.return_value.json.return_value = clean_inspection()
    factory = Mock(return_value=client)
    monkeypatch.setattr('seeker.media.httpx.Client', factory)
    path = tmp_path / 'clip.mp4'
    path.write_bytes(b'private')
    settings = Mock(sightengine_user='local', sightengine_secret='local')
    inspect_video(path, settings)
    assert factory.call_args.kwargs['follow_redirects'] is False
    assert factory.call_args.kwargs['trust_env'] is False
    args, kwargs = client.post.call_args
    assert args == (SIGHTENGINE_VIDEO_ENDPOINT,)
    assert args[0].endswith('/1.0/video/check-sync.json')
    assert 'url' not in kwargs['data']
    assert kwargs['data']['models'] == 'face-analysis,text'
    assert kwargs['files']['media'][0] == 'clip.mp4'
    client.post.return_value.json.return_value = {"status": "success"}
    with pytest.raises(MediaError, match='inspection_failed'):
        inspect_video(path, settings)


def box(kind, body=b''):
    return struct.pack('>I4s', 8 + len(body), kind) + body


def test_metadata_stripped_through_entire_box_tree_and_sample_entry():
    secret = b'PRIVATE_SOURCE_LABEL'
    leaf = box(b'uuid', secret) + box(b'udta', secret) + box(b'Xtra', secret) + box(b'free', secret)
    entry = bytearray(78)
    entry[42:42+len(secret)] = secret
    track = box(b'trak', box(b'mdia', box(b'minf', box(b'stbl',
        box(b'stsd', bytes(8) + box(b'avc1', bytes(entry) + leaf)) + leaf)) + leaf))
    source = box(b'moov', track + box(b'edts', leaf)) + leaf + box(b'abcd', secret)
    output = strip_metadata(source)
    assert secret not in output
    assert len(output) == len(source)
    assert strip_metadata(output) == output
    with pytest.raises(MediaError):
        strip_metadata(struct.pack('>I4s', 100, b'moov') + b'bad')
    with pytest.raises(MediaError):
        strip_metadata(box(b'dinf', box(b'dref', bytes(8) + box(b'url ', b'\0\0\0\0https://remote'))))


def generate(directory, pattern='testsrc2', crf='20', metadata='opaque'):
    sandbox(directory, ['/usr/bin/ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        f'{pattern}=size=320x180:rate=24:duration=2', '-c:v', 'libx264', '-threads', '1',
                        '-crf', crf, '-metadata', f'title={metadata}', '-y', 'input.mp4'])
    return directory / 'input.mp4'


def test_real_sandbox_transcode_strips_metadata_and_hashes_pixels(tmp_path):
    source = generate(tmp_path, metadata='PRIVATE_SOURCE_LABEL')
    first, a = transcode(source, tmp_path)
    assert b'PRIVATE_SOURCE_LABEL' not in first
    assert b'Lavf' not in first and b'Lavc' not in first and b'x264 -' not in first
    source = generate(tmp_path, crf='23', metadata='another')
    second, b = transcode(source, tmp_path)
    assert first != second
    assert len(json.loads(a['dhashes'])) == 96
    assert perceptual_duplicate(json.loads(a['dhashes']), json.loads(b['dhashes']))
    source = generate(tmp_path, pattern='smptebars')
    _, c = transcode(source, tmp_path)
    assert not perceptual_duplicate(json.loads(a['dhashes']), json.loads(c['dhashes']))
    assert a['duration_ms'] == 4000 and (a['width'], a['height'], a['fps']) == (640, 360, 24)


def test_dhash_compares_decoded_pixel_neighbors():
    ascending = bytes(range(9)) * 8
    descending = bytes(reversed(range(9))) * 8
    assert dhash(ascending) == '0000000000000000'
    assert dhash(descending) == 'ffffffffffffffff'
    assert perceptual_duplicate([dhash(ascending)], [dhash(bytes(x + 10 for x in ascending))])
    assert not perceptual_duplicate([dhash(ascending)], [dhash(descending)])


@pytest.fixture
def video(pilot, tmp_path):
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = False
    settings.media_root = tmp_path / 'private'
    settings.media_operators = [str(keys[0].pubkey())]
    settings.media_reviewers = [str(keys[1].pubkey())]
    settings.media_signing_secret = 'test-only-secret-' * 3
    with app.state.database.transaction() as db:
        for key in keys:
            db.get(Actor, str(key.pubkey())).bonded = True
    return pilot


def seed_video(video, status='review'):
    c, app, h, keys, chain, settings = video
    aid, vid, jid = uid(), uid(), uid()
    data = box(b'ftyp', b'isom') + box(b'mdat', bytes.fromhex(vid) * 32)
    sha = hashlib.sha256(data).hexdigest()
    with app.state.database.transaction() as db:
        db.add(MediaAsset(asset_id=aid, status=status))
        db.flush()
        db.add(IngestJob(job_id=jid, asset_id=aid, status=status))
        db.add(MediaVariant(variant_id=vid, asset_id=aid, sha256=sha, dhashes='[]',
                            duration_ms=4000, width=640, height=360, fps=24, byte_size=len(data)))
    write_private(asset_path(settings, 'review', vid), data)
    return aid, vid, sha, data


def approve(c, header, aid, sha):
    return c.post(f'/media/assets/{aid}/approve', headers=header, json={
        'sha256': sha, 'watched_entire_clip': True, 'neutral_content': True, 'no_identifying_content': True})


def commissioned(video):
    c, app, h, *_ = video
    a, b = seed_video(video), seed_video(video)
    for aid, vid, sha, data in (a, b):
        assert approve(c, h[1], aid, sha).status_code == 200
    result = c.post('/media/pairs', headers=h[0], json={'asset_a_id': a[0], 'asset_b_id': b[0]})
    assert result.status_code == 200, result.text
    return result.json(), a, b


def test_operator_only_ingest_private_and_human_gate(video):
    c, app, h, keys, chain, settings = video
    data = box(b'ftyp', b'isom')
    assert c.post('/media/ingest', headers={'Content-Type': 'video/mp4'}, content=data).status_code == 403
    assert c.post('/media/ingest', headers={**h[2], 'Content-Type': 'video/mp4'}, content=data).status_code == 403
    result = c.post('/media/ingest', headers={**h[0], 'Content-Type': 'video/mp4'}, content=data)
    assert result.status_code == 202
    aid = result.json()['asset_id']
    assert asset_path(settings, 'quarantine', aid).read_bytes() == data
    assert c.get('/media/ingest/' + aid, headers=h[2]).status_code == 403
    assert approve(c, h[1], aid, '0' * 64).status_code == 409
    a, b = seed_video(video), seed_video(video)
    assert c.post('/media/pairs', headers=h[0], json={'asset_a_id': a[0], 'asset_b_id': b[0]}).status_code == 404
    assert approve(c, h[0], a[0], a[2]).status_code == 403
    assert approve(c, h[1], a[0], '0' * 64).status_code == 409
    assert c.post('/media/assets/' + a[0] + '/approve', headers=h[1], json={
        'sha256': a[2], 'watched_entire_clip': False, 'neutral_content': True, 'no_identifying_content': True}).status_code == 422
    bad = c.post('/media/pairs', headers=h[0], json={'source_url': 'http://127.0.0.1/private'})
    assert bad.status_code == 422 and '127.0.0.1' not in bad.text


def test_signed_delivery_range_expiry_revocation_wallet_and_tamper(video, monkeypatch):
    c, app, h, keys, chain, settings = video
    pair, a, b = commissioned(video)
    url = pair['output_a_url']
    assert c.get(url).status_code == 401
    assert c.get(url, headers=h[2]).status_code == 403
    assert c.get(url + 'x', headers=h[0]).status_code == 403
    response = c.get(url, headers={**h[0], 'Range': 'bytes=8-19'})
    assert response.status_code == 206 and response.content == a[3][8:20]
    assert response.headers['content-range'] == f'bytes 8-19/{len(a[3])}'
    assert response.headers['cache-control'] == 'private, no-store'
    assert c.head(url, headers=h[0]).status_code == 200
    assert c.get(url, headers={**h[0], 'Range': 'bytes=999999-'}).status_code == 416
    with monkeypatch.context() as patch:
        patch.setattr('seeker.media.now', lambda: pair['media_a']['expires_at'])
        assert c.get(url, headers=h[0]).status_code == 403
    assert c.post('/media/assets/' + a[0] + '/revoke', headers=h[1]).status_code == 200
    assert c.get(url, headers=h[0]).status_code == 404
    assert c.head(url, headers=h[0]).status_code == 404
    assert c.get('/judge/next?modality=video', headers=h[2]).json()['kind'] == 'none'


def test_old_clients_image_only_video_skip_unpaid_and_view_gate(video):
    c, app, h, keys, chain, settings = video
    pair, *_ = commissioned(video)
    assert c.get('/judge/next', headers=h[2]).json()['kind'] == 'none'
    image_id = seed_pair(app)
    assert c.get('/judge/next', headers=h[2]).json()['pair_id'] == image_id
    delivered = c.get('/judge/next?modality=video', headers=h[2]).json()
    pid = delivered['pair_id']
    assert c.post('/judge/' + pid, headers=h[2], json={'choice': 'cant_tell'}).status_code == 409
    assert c.get(delivered['output_a_url'], headers=h[2]).status_code == 200
    assert c.post('/judge/' + pid, headers=h[2], json={'choice': 'a'}).status_code == 409
    assert c.get(delivered['output_b_url'], headers=h[2]).status_code == 200
    assert c.post('/judge/' + pid, headers=h[2], json={'choice': 'a'}).status_code == 409
    result = c.post('/judge/' + pid + '/media-skip', headers=h[2])
    assert result.json() == {'status': 'skipped', 'paid': False}
    assert c.get('/judge/next?modality=video', headers=h[2]).json()['kind'] == 'none'
    with app.state.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(Judgment)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        access = db.get(MediaAccess, (str(keys[2].pubkey()), pid))
        access.served_a_at = access.served_b_at = 0
    assert c.post('/judge/' + pid, headers=h[2], json={'choice': 'cant_tell'}).status_code == 409
    assert not chain.reservations and not chain.settled_skr


def test_video_lost_reservation_and_fulfillment_callbacks_reconcile(video, monkeypatch):
    c, app, h, keys, chain, settings = video
    settings.judge_k = 1
    pair, *_ = commissioned(video)
    pid = pair['pair_id']
    with app.state.database.transaction() as db:
        access = db.get(MediaAccess, (str(keys[0].pubkey()), pid))
        access.served_a_at = access.served_b_at = 0
    result = c.post('/judge/' + pid, headers=h[0], json={'choice': 'a'})
    assert result.status_code == 202
    jid = result.json()['judgment_id']
    original_reserve = chain.reserve_judgment
    def lost_reserve(*args):
        original_reserve(*args)
        raise RuntimeError('callback lost')
    monkeypatch.setattr(chain, 'reserve_judgment', lost_reserve)
    run_work(app)
    assert chain.reservations
    monkeypatch.setattr(chain, 'reserve_judgment', original_reserve)
    original_resolve = chain.resolve_judgment
    def lost_resolve(*args):
        original_resolve(*args)
        raise RuntimeError('callback lost')
    monkeypatch.setattr(chain, 'resolve_judgment', lost_resolve)
    run_work(app)
    assert not chain.reservations
    monkeypatch.setattr(chain, 'resolve_judgment', original_resolve)
    run_work(app)
    with app.state.database.transaction() as db:
        j = db.get(Judgment, jid)
        assert j.enforcement_complete  # reservation flag is a historical receipt
        assert j.payout_signature
    assert len(chain.settled_skr) == 1


def test_real_ingest_worker_to_approved_playback_and_duplicate(video, tmp_path, monkeypatch):
    c, app, h, keys, chain, settings = video
    monkeypatch.setattr('seeker.media_worker.inspect_video', lambda *args: None)
    worker = MediaWorker(app.state.database, settings)
    assets = []
    for pattern in ['testsrc2', 'smptebars']:
        data = generate(tmp_path, pattern=pattern).read_bytes()
        result = c.post('/media/ingest', headers={**h[0], 'Content-Type': 'video/mp4'}, content=data)
        assert result.status_code == 202, result.text
        aid = result.json()['asset_id']
        assert worker.run_once()
        status = c.get('/media/ingest/' + aid, headers=h[1]).json()
        assert status['status'] == 'review', status
        assert approve(c, h[1], aid, status['sha256']).status_code == 200
        assets.append(aid)
    result = c.post('/media/pairs', headers=h[0], json={'asset_a_id': assets[0], 'asset_b_id': assets[1]})
    assert result.status_code == 200
    response = c.get(result.json()['output_a_url'], headers=h[0])
    assert response.status_code == 200 and response.content[4:8] == b'ftyp'
    data = generate(tmp_path, pattern='testsrc2', crf='23').read_bytes()
    result = c.post('/media/ingest', headers={**h[0], 'Content-Type': 'video/mp4'}, content=data)
    assert worker.run_once()
    status = c.get('/media/ingest/' + result.json()['asset_id'], headers=h[0]).json()
    assert status['status'] == 'failed' and status['error_code'] == 'duplicate'


def test_failed_inspection_never_reaches_review(video, tmp_path, monkeypatch):
    c, app, h, keys, chain, settings = video
    data = generate(tmp_path).read_bytes()
    aid = c.post('/media/ingest', headers={**h[0], 'Content-Type': 'video/mp4'}, content=data).json()['asset_id']
    def fail(*args):
        raise MediaError('inspection_failed')
    monkeypatch.setattr('seeker.media_worker.inspect_video', fail)
    assert MediaWorker(app.state.database, settings).run_once()
    status = c.get('/media/ingest/' + aid, headers=h[0]).json()
    assert status['status'] == 'failed' and status['error_code'] == 'inspection_failed'
    assert not list(bucket(settings, 'review').iterdir())
    assert approve(c, h[1], aid, '0' * 64).status_code == 409


def test_revocation_releases_collecting_video_reservations_without_pay(video):
    c, app, h, keys, chain, settings = video
    settings.judge_k = 3
    pair, a, _ = commissioned(video)
    pid = pair['pair_id']
    with app.state.database.transaction() as db:
        access = db.get(MediaAccess, (str(keys[0].pubkey()), pid))
        access.served_a_at = access.served_b_at = 0
    response = c.post('/judge/' + pid, headers=h[0], json={'choice': 'a'})
    assert response.status_code == 202
    assert c.post('/judge/' + pid + '/media-skip', headers=h[0]).status_code == 409
    run_work(app)
    assert chain.reservations
    assert c.post('/media/assets/' + a[0] + '/revoke', headers=h[1]).status_code == 200
    run_work(app)
    assert not chain.reservations and not chain.settled_skr
    with app.state.database.transaction() as db:
        j = db.get(Judgment, response.json()['judgment_id'])
        assert j.enforcement_complete and not j.enforcement_penalty and j.earned_skr == 0
        actor = db.get(Actor, str(keys[0].pubkey()))
        assert actor.judged_count == 0


def test_approval_and_serving_detect_derivative_tampering(video):
    c, app, h, keys, chain, settings = video
    a = seed_video(video)
    asset_path(settings, 'review', a[1]).write_bytes(b'changed')
    assert approve(c, h[1], a[0], a[2]).status_code == 409
    pair, a, _ = commissioned(video)
    path = asset_path(settings, 'approved', a[1])
    manifest = json.loads(path.with_suffix('.json').read_bytes())
    assert manifest['asset_id'] == a[0] and manifest['sha256'] == a[2]
    assert 'source' not in json.dumps(manifest)
    path.chmod(0o600)
    path.write_bytes(b'changed')
    assert c.get(pair['output_a_url'], headers=h[0]).status_code == 404


def test_ingest_limits_and_sandbox_fail_closed(video, tmp_path, monkeypatch):
    c, app, h, keys, chain, settings = video
    settings.media_max_bytes = 64
    assert c.post('/media/ingest', headers={**h[0], 'Content-Type': 'video/mp4'}, content=b'x' * 65).status_code == 413
    assert c.post('/media/ingest', headers={**h[0], 'Content-Type': 'image/png'}, content=box(b'ftyp', b'isom')).status_code == 415
    assert c.post('/media/ingest', headers={**h[0], 'Content-Type': 'video/mp4'}, content=b'x' * 20).status_code == 422
    def failure(*args, **kwargs):
        raise OSError('PRIVATE_SOURCE_LABEL')
    monkeypatch.setattr('seeker.media_ingest.subprocess.run', failure)
    with pytest.raises(MediaError) as caught:
        sandbox(tmp_path, ['/usr/bin/true'])
    assert str(caught.value) == 'processing_failed'


def test_revocation_during_inspection_cannot_be_overwritten(video, tmp_path, monkeypatch):
    c, app, h, keys, chain, settings = video
    data = generate(tmp_path).read_bytes()
    aid = c.post('/media/ingest', headers={**h[0], 'Content-Type': 'video/mp4'}, content=data).json()['asset_id']
    def revoke_while_inspecting(*args):
        assert c.post('/media/assets/' + aid + '/revoke', headers=h[1]).status_code == 200
    monkeypatch.setattr('seeker.media_worker.inspect_video', revoke_while_inspecting)
    assert MediaWorker(app.state.database, settings).run_once()
    assert c.get('/media/ingest/' + aid, headers=h[0]).json()['status'] == 'revoked'
    with app.state.database.transaction() as db:
        assert db.get(MediaAsset, aid).status == 'revoked'
        assert db.scalar(select(MediaVariant).where(MediaVariant.asset_id == aid)) is None
