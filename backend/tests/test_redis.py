"""Tests against a real Redis (run inside the compose network: `make test`). Skipped if unreachable."""
import asyncio
import os
import time
import uuid

import pytest
import redis.asyncio as aioredis

from notetaker.models import Status
from notetaker.ratelimit import Bucket, RateLimiter
from notetaker.store import JobStore

REDIS_URL = os.getenv("TEST_REDIS_URL", os.getenv("REDIS_URL", "redis://localhost:6379/15")).rsplit("/", 1)[0] + "/15"


async def _reachable() -> bool:
    try:
        r = aioredis.from_url(REDIS_URL)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store():
    if not await _reachable():
        pytest.skip("redis not reachable")
    s = JobStore(REDIS_URL)
    await s.r.flushdb()
    yield s
    await s.r.flushdb()
    await s.close()


async def _new(store):
    jid, created = await store.create_job(video_id="dQw4w9WgXcQ", youtube_url="https://youtu.be/dQw4w9WgXcQ",
                                          notion_token="secret_" + "a" * 30, notion_parent_id=None, idempotency_key=None)
    assert created
    return jid


async def test_lua_state_machine_rejects_illegal_transitions(store):
    jid = await _new(store)
    assert (await store.commit(jid, target=Status.SUMMARIZING)).code == "INVALID"   # can't skip
    assert (await store.commit(jid, target=Status.TRANSCRIBING)).ok
    assert (await store.commit(jid, target=Status.TRANSCRIBING)).ok                 # idempotent same-state
    assert (await store.commit(jid, target=Status.QUEUED)).code == "INVALID"        # no going back
    for s in (Status.SUMMARIZING, Status.EXTRACTING_FRAMES, Status.PUBLISHING, Status.COMPLETED):
        assert (await store.commit(jid, target=s)).ok
    assert (await store.commit(jid, target=Status.FAILED)).code == "INVALID"        # terminal is a sink
    assert (await store.set_fields(jid, progress=5)).code == "TERMINAL"            # late writes refused
    view = await store.view(jid)
    assert [t["status"] for t in view["timeline"]] == ["QUEUED", "TRANSCRIBING", "SUMMARIZING",
                                                        "EXTRACTING_FRAMES", "PUBLISHING", "COMPLETED"]
    assert not await store.r.hexists(store.k(jid), "token_enc")  # token wiped at terminal state


async def test_lease_fencing_blocks_zombie_writer(store):
    jid = await _new(store)
    assert await store.acquire_lease(jid, "transcribe", "A")
    assert not await store.acquire_lease(jid, "transcribe", "B")
    await store.r.delete(f"lock:{jid}:transcribe")          # simulate A's lease expiring
    assert await store.acquire_lease(jid, "transcribe", "B")
    res = await store.commit(jid, target=Status.TRANSCRIBING, lease=(f"lock:{jid}:transcribe", "A"))
    assert res.code == "LOST_LEASE"                          # zombie A can't write
    assert (await store.commit(jid, target=Status.TRANSCRIBING, lease=(f"lock:{jid}:transcribe", "B"))).ok


async def test_stage_commit_is_atomic_with_checkpoint(store):
    jid = await _new(store)
    await store.commit(jid, target=Status.TRANSCRIBING)
    res = await store.commit(jid, target=Status.SUMMARIZING, outputs={"transcript": {"segments": [1, 2]}},
                             done_stage="transcribe", fields={"video_title": "T"})
    assert res.ok
    assert await store.done_stages(jid) == {"transcribe"}
    assert await store.load_ckpt(jid, "transcript") == {"segments": [1, 2]}
    assert (await store.get_raw(jid))["video_title"] == "T"


async def test_crash_marker_counts_crashes(store):
    jid = await _new(store)
    assert await store.mark_running(jid, "frames", "w1") == 0
    # w1 dies without clearing → next runner sees a crash
    assert await store.mark_running(jid, "frames", "w2") == 1
    await store.clear_running(jid, "frames")
    assert await store.mark_running(jid, "frames", "w3") == 0


async def test_idempotency_key_dedupes(store):
    a, c1 = await store.create_job(video_id="dQw4w9WgXcQ", youtube_url="u", notion_token="secret_" + "a" * 30,
                                   notion_parent_id=None, idempotency_key="k1")
    b, c2 = await store.create_job(video_id="dQw4w9WgXcQ", youtube_url="u", notion_token="secret_" + "a" * 30,
                                   notion_parent_id=None, idempotency_key="k1")
    assert a == b and c1 and not c2


async def test_gcra_spaces_requests_across_clients(store):
    """Two 'workers' sharing one bucket at 120/min (one every 0.5 s) can't exceed the rate."""
    limiter_a, limiter_b = RateLimiter(store.r), RateLimiter(store.r)
    bucket = [Bucket(f"rl:test:{uuid.uuid4().hex}", rate_per_sec=2.0, burst=1)]
    admitted: list[float] = []

    async def worker(lim):
        for _ in range(3):
            await lim.acquire("test", bucket, max_wait=10)
            admitted.append(time.monotonic())

    t0 = time.monotonic()
    await asyncio.gather(worker(limiter_a), worker(limiter_b))
    admitted.sort()
    gaps = [b - a for a, b in zip(admitted, admitted[1:])]
    assert len(admitted) == 6
    assert min(gaps) >= 0.40, gaps                 # ≥ emission interval (minus jitter)
    assert admitted[-1] - t0 >= 2.3                # 6 requests at 2/s ≈ 2.5 s


async def test_gcra_multi_bucket_is_all_or_nothing(store):
    lim = RateLimiter(store.r)
    rpm = Bucket(f"rl:t:rpm:{uuid.uuid4().hex}", rate_per_sec=100, burst=100)
    tpm = Bucket(f"rl:t:tpm:{uuid.uuid4().hex}", rate_per_sec=10, burst=100)
    assert await lim.try_acquire([rpm, tpm], [1, 90]) == 0
    wait = await lim.try_acquire([rpm, tpm], [1, 50])   # token bucket can't fit → nothing charged
    assert wait > 0
    assert int(float(await store.r.get(rpm.key))) > 0   # rpm only charged once (first call)


async def test_cooldown_blocks_everyone(store):
    lim = RateLimiter(store.r)
    await lim.set_cooldown("svc", 30)
    from notetaker.errors import RateLimited
    with pytest.raises(RateLimited):
        await lim.acquire("svc", [Bucket("rl:t:x", 100, 100)], max_wait=1)


async def test_metrics_mix_int_counters_and_float_durations(store):
    await store.r.hincrby("metrics", "llm_calls", 3)
    await store.r.hincrbyfloat("metrics", "ratelimit_wait_sec:llm", 30.656)
    m = await store.metrics()
    assert m["llm_calls"] == 3 and isinstance(m["llm_calls"], int)
    assert m["ratelimit_wait_sec:llm"] == 30.656


async def test_cross_video_concept_index(store):
    from notetaker.graph import forget_page, register_page, related_pages
    g1 = {"concepts": [{"name": "Neuron", "definition": "holds an activation"},
                       {"name": "Sigmoid", "definition": "squashing function"}]}
    g2 = {"concepts": [{"name": "Neurons", "definition": "units of a network"},
                       {"name": "Backprop", "definition": "computes gradients of the cost"}]}
    tok = "ntn_test_" + "x" * 30
    await register_page(store.r, tok, "vid1", {"id": "p1", "url": "u1"}, "Video 1", g1)
    await register_page(store.r, tok, "vid1", {"id": "p1b", "url": "u1"}, "Video 1", g1)  # replay overwrites
    rel = await related_pages(store.r, tok, "vid2", g2)
    assert len(rel) == 1 and rel[0]["page_id"] == "p1b" and rel[0]["shared"] == ["Neuron"]
    # Different names, same idea: a concept of one video appearing in the other's definitions counts.
    g3 = {"concepts": [{"name": "Gradient descent", "definition": "follows the gradient to minimise the cost functions"}]}
    g4 = {"concepts": [{"name": "Cost function", "definition": "measures error"}]}
    await register_page(store.r, tok, "vid3", {"id": "p3", "url": "u3"}, "Video 3", g3)
    rel = await related_pages(store.r, tok, "vid4", g4)
    assert [e["video_id"] for e in rel] == ["vid3"] and rel[0]["shared"] == ["Cost function"]
    await forget_page(store.r, tok, "vid3")
    assert await related_pages(store.r, tok, "vid1", g1) == []  # never links to itself
    await forget_page(store.r, tok, "vid1")
    assert await related_pages(store.r, tok, "vid2", g2) == []


async def test_transcript_cache_round_trip(store):
    vid = "cache" + uuid.uuid4().hex[:6]
    assert await store.get_cached_transcript(vid) is None
    await store.cache_transcript(vid, {"segments": [{"start": 0, "dur": 1, "text": "hi"}], "info": {"source": "x"}})
    got = await store.get_cached_transcript(vid)
    assert got["segments"][0]["text"] == "hi" and 0 < await store.r.ttl(f"transcript:{vid}") <= 7 * 86400
    await store.cache_transcript(vid + "e", {"segments": [], "info": {}})  # empty transcripts are never cached
    assert await store.get_cached_transcript(vid + "e") is None
