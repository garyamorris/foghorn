"""Replay engine on the virtual clock: speed, pause/resume/step, loop, backpressure, ids, jitter, scenario launches."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from foghorn.events.dispatcher import DispatchError
from foghorn.events.vclock import parse_iso
from tests.unit.events.fakes import BASE, scenario_events

FIVE = BASE + timedelta(minutes=5)


async def finish(env, timeout: float = 5.0) -> None:  # noqa: ASYNC109
    await asyncio.wait_for(env.rt.replay.session.task, timeout)  # type: ignore[union-attr]


async def test_scenario_replay_publishes_every_event_through_ingest_in_virtual_time_order(env):
    env.data.events["S01"] = scenario_events("S01", 6)
    s = await env.rt.replay.start(scenario_id="S01", speed=60)
    assert s["state"] == "running" and s["total"] == 6 and s["kind"] == "scenario" and s["launch_id"] == "lau_0001"
    await finish(env)
    assert env.rt.replay.status()["state"] == "finished" and env.rt.replay.session.position == 6  # type: ignore[union-attr]
    rows = await env.rt.db.run(env.rt.db.list_events, types=["alert.fired", "deploy.finished"], order="asc")
    assert [r["id"] for r in rows] == [f"evt_S01_{i:05d}" for i in range(1, 7)]  # deterministic evt_<name>_<seq>
    occ = [parse_iso(r["occurred_at"]) for r in rows]
    assert occ == sorted(occ) and (occ[-1] - occ[0]).total_seconds() == 150
    assert all(r["scenario_id"] == "S01" and r["correlation_id"] == "corr_S01_lau_0001" for r in rows)
    types = [r["type"] for r in await env.rt.db.run(env.rt.db.list_events, types=["replay.*"], order="asc")]
    assert types == ["replay.started", "replay.finished"]
    # virtual time moved at 60x: 150 virtual seconds took ~2.5 fake-wall seconds
    assert abs((env.rt.vclock.now() - parse_iso("2026-11-05T10:07:30Z")).total_seconds()) < 5
    assert 2.0 <= sum(env.wall.sleeps) <= 4.0


@pytest.mark.parametrize("speed", [1, 10, 60, 600])
async def test_speeds_scale_the_wall_time_needed(env, speed):
    env.data.events["S01"] = scenario_events("S01", 3, step_s=60)
    before = env.wall.t
    await env.rt.replay.start(scenario_id="S01", speed=speed)
    await finish(env)
    elapsed = (env.wall.t - before).total_seconds()
    assert elapsed == pytest.approx(120 / speed, abs=0.6)  # 2 virtual minutes between first and last event


async def test_virtual_clock_is_monotonic_and_per_component_order_is_preserved(env):
    env.data.events["S01"] = scenario_events("S01", 10)
    seen = []
    orig = env.rt.bus.ingest

    async def spy(raw, **kw):
        seen.append((env.rt.vclock.now(), raw["occurred_at"]))
        return await orig(raw, **kw)

    env.rt.bus.ingest = spy  # type: ignore[method-assign]
    await env.rt.replay.start(scenario_id="S01", speed=600)
    await finish(env)
    clocks = [c for c, _ in seen[1:-1]]
    assert clocks == sorted(clocks)
    for c, occ in seen[1:-1]:
        assert c >= parse_iso(occ) - timedelta(milliseconds=1)  # never released early
    by_comp: dict[str, list[str]] = {}
    for r in await env.rt.db.run(env.rt.db.list_events, types=["alert.fired", "deploy.finished"], order="asc"):
        by_comp.setdefault(r["component"], []).append(r["occurred_at"])
    assert all(v == sorted(v) for v in by_comp.values())


async def test_pause_resume_step_and_speed_controls(env):
    env.data.events["S01"] = scenario_events("S01", 8, step_s=60)
    await env.rt.replay.start(scenario_id="S01", speed=1)
    r = env.rt.replay
    await asyncio.sleep(0.01)
    await r.pause()
    assert r.status()["state"] == "paused" and env.rt.vclock.paused
    await asyncio.sleep(0.02)
    pos = r.session.position  # type: ignore[union-attr]
    await asyncio.sleep(0.05)
    assert r.session.position == pos  # frozen while paused
    vt = env.rt.vclock.now()
    await r.step()
    await asyncio.sleep(0.05)
    assert r.session.position == pos + 1 and r.status()["state"] == "paused"  # exactly one event released
    assert env.rt.vclock.now() >= vt
    await r.step()
    await r.step()
    await asyncio.sleep(0.05)
    assert r.session.position == pos + 3
    await r.set_speed(600)
    assert env.rt.vclock.speed == 600 and r.session.speed == 600  # type: ignore[union-attr]
    await r.resume()
    assert not env.rt.vclock.paused
    await finish(env)
    assert r.status()["state"] == "finished" and r.session.position == 8  # type: ignore[union-attr]
    with pytest.raises(DispatchError):
        await r.pause()  # nothing active any more
    with pytest.raises(DispatchError):
        await r.set_speed(0)


async def test_stop_halts_the_session_and_emits_replay_finished(env):
    env.data.events["S01"] = scenario_events("S01", 8, step_s=600)
    await env.rt.replay.start(scenario_id="S01", speed=1)
    await asyncio.sleep(0.01)
    out = await env.rt.replay.stop()
    assert out["state"] == "stopped" and not env.rt.replay.active
    fin = await env.rt.db.run(env.rt.db.list_events, types=["replay.finished"])
    assert fin[0]["payload"]["state"] == "stopped"
    assert (await env.rt.replay.stop())["state"] == "stopped"
    assert (await _fresh_idle(env))["state"] == "idle"


async def _fresh_idle(env):
    env.rt.replay.session = None
    return env.rt.replay.status()


async def test_only_one_session_unless_replaced_and_inputs_are_validated(env):
    env.data.events["S01"] = scenario_events("S01", 4, step_s=3600)
    await env.rt.replay.start(scenario_id="S01", speed=1)
    with pytest.raises(DispatchError) as e:
        await env.rt.replay.start(scenario_id="S01", speed=1)
    assert e.value.status == 409 and e.value.code == "replay_active"
    s = await env.rt.replay.start(stream="stream_quiet_hour", speed=600, replace=True)
    assert s["kind"] == "stream" and s["name"] == "stream_quiet_hour" and s["id"] == "rep_0002"
    await finish(env)
    for kw in ({}, {"stream": "a", "scenario_id": "b"}, {"stream": "stream_quiet_hour", "speed": 601}):
        with pytest.raises(DispatchError) as bad:
            await env.rt.replay.start(**kw)
        assert bad.value.status == 400
    with pytest.raises(DispatchError) as empty:
        await env.rt.replay.start(stream="stream_storm")
    assert empty.value.status == 404
    with pytest.raises(KeyError):
        await env.rt.replay.start(stream="nope")


async def test_rerunning_a_scenario_gets_fresh_ids_and_its_own_dedupe_scope(env):
    env.data.events["S01"] = scenario_events("S01", 2)
    await env.rt.replay.start(scenario_id="S01", speed=600)
    await finish(env)
    await env.rt.replay.start(scenario_id="S01", speed=600)
    await finish(env)
    ids = [
        r["id"]
        for r in await env.rt.db.run(env.rt.db.list_events, types=["alert.fired", "deploy.finished"], order="asc")
    ]
    assert ids == ["evt_S01_00001", "evt_S01_00002", "evt_S01_00001_r2", "evt_S01_00002_r2"]
    statuses = {
        r["status"] for r in await env.rt.db.run(env.rt.db.list_events, types=["alert.fired", "deploy.finished"])
    }
    assert "suppressed" not in statuses


async def test_loop_restarts_the_stream_and_virtual_time_keeps_moving_forward(env):
    env.data.events["S01"] = scenario_events("S01", 3, step_s=30)
    await env.rt.replay.start(scenario_id="S01", speed=600, loop=True)
    while env.rt.replay.session.loop_index < 2:  # type: ignore[union-attr]  # noqa: ASYNC110
        await asyncio.sleep(0.005)
    await env.rt.replay.stop()
    rows = await env.rt.db.run(env.rt.db.list_events, types=["alert.fired", "deploy.finished"], order="asc")
    ids = [r["id"] for r in rows]
    assert "evt_S01_00001_L1" in ids and len(set(ids)) == len(ids)
    occ = [parse_iso(r["occurred_at"]) for r in rows]
    assert occ == sorted(occ) and occ[3] > occ[2]


async def test_start_at_skips_earlier_events(env):
    env.data.events["S01"] = scenario_events("S01", 6)
    s = await env.rt.replay.start(scenario_id="S01", speed=600, start_at=(FIVE + timedelta(seconds=60)).isoformat())
    assert s["total"] == 4
    await finish(env)


async def test_jitter_reorders_delivery_across_components_but_never_within_one(env):
    base = scenario_events("S01", 40, step_s=1)
    env.data.events["S01"] = base
    await env.rt.replay.start(scenario_id="S01", speed=600, jitter_s=5)
    await finish(env)
    rows = await env.rt.db.run(env.rt.db.list_events, types=["alert.fired", "deploy.finished"], order="asc")
    delivered = [r["id"] for r in rows]
    assert sorted(delivered) == sorted(e["id"] for e in base) and delivered != [
        e["id"] for e in base
    ]  # out of order overall
    by_comp: dict[str, list[int]] = {}
    for r in rows:
        by_comp.setdefault(r["component"], []).append(int(r["id"][-5:]))
    assert all(v == sorted(v) for v in by_comp.values())
    # deterministic: the same seed yields the same delivery order
    env.rt.replay.session = None
    again = env.rt.replay._prepare(base, None, 5, seed="scenario:S01")  # noqa: SLF001
    assert [e["id"] for _d, _o, e in again] == delivered


async def test_replay_pauses_the_clock_at_high_water_and_resumes_at_low_water(make_env):
    env = await make_env(queue_cap_p0=2, queue_cap_p1=2, queue_cap_p2=10, queue_cap_p3=2, high_water=0.5, low_water=0.3)
    env.data.events["S01"] = [
        dict(e, type="customer.report", severity="medium", payload={"n": i}, dedupe_key=None)
        for i, e in enumerate(scenario_events("S01", 20, step_s=1))
    ]
    await env.rt.replay.start(scenario_id="S01", speed=600)
    for _ in range(300):
        if env.rt.replay.session.bp_paused:  # type: ignore[union-attr]
            break
        await asyncio.sleep(0.005)
    assert env.rt.replay.session.bp_paused and env.rt.queues.bp_active and env.rt.vclock.paused  # type: ignore[union-attr]
    frozen = env.rt.replay.session.position  # type: ignore[union-attr]
    await asyncio.sleep(0.05)
    assert env.rt.replay.session.position == frozen  # no flooding while backpressured
    env.rt.dispatcher.pump()  # drain the queues: the dispatcher catches up
    for _ in range(400):
        env.rt.dispatcher.pump()
        if env.rt.replay.session.state == "finished":  # type: ignore[union-attr]
            break
        await asyncio.sleep(0.005)
    assert env.rt.replay.session.state == "finished"  # type: ignore[union-attr]
    assert env.rt.queues.shed_total == 0 and await env.rt.db.run(env.rt.db.count_events) >= 20


async def test_replay_event_that_hits_429_waits_and_retries(make_env):
    env = await make_env()
    calls = {"n": 0}
    orig = env.rt.bus.ingest

    async def flaky(raw, **kw):
        if raw["type"] == "alert.fired":
            calls["n"] += 1
            if calls["n"] == 1:
                from foghorn.events.pipeline import IngestResult

                return IngestResult(
                    429, "backpressure", retry_after=1, error={"code": "backpressure", "message": "x", "details": None}
                )
        return await orig(raw, **kw)

    env.rt.bus.ingest = flaky  # type: ignore[method-assign]
    env.data.events["S01"] = scenario_events("S01", 1)
    await env.rt.replay.start(scenario_id="S01", speed=600)
    await asyncio.wait_for(env.rt.replay.session.task, 5)  # type: ignore[union-attr]
    assert calls["n"] == 2 and env.rt.replay.status()["state"] == "finished"


async def test_scenario_events_carry_launch_context_into_the_dispatched_runs(env):
    env.data.events["S01"] = scenario_events("S01", 2)
    await env.rt.replay.start(
        scenario_id="S01",
        speed=600,
        run_ctx={"model_profile": "naive", "approval_policy": "strict", "breakpoints": ["approval_gate"]},
    )
    await finish(env)
    await env.idle()
    runs = env.client.of("runs.create")
    assert runs and all(
        r["context"]["model_profile"] == "naive"
        and r["context"]["approval_policy"] == "strict"
        and r["context"]["breakpoints"] == ["approval_gate"]
        and r["context"]["scenario_id"] == "S01"
        for r in runs
    )
    assert runs[0]["metadata"]["foghorn_scenario_id"] == "S01" and runs[0]["metadata"]["foghorn_trigger"] == "replay"


async def test_ambient_generator_is_off_by_default_and_marks_events(make_env):
    env = await make_env()
    assert not env.rt.ambient.running and env.rt.es.ambient_enabled is False
    assert await env.rt.ambient.set_enabled(True) is True
    for _ in range(200):
        if env.rt.ambient.sent >= 3:
            break
        await asyncio.sleep(0.005)
    await env.rt.ambient.set_enabled(False)
    assert not env.rt.ambient.running and env.rt.ambient.sent >= 3
    row = (await env.rt.db.run(env.rt.db.list_events, source="replay:stream_quiet_hour", limit=1))[0]
    assert row["attributes"]["ambient"] is True
    await env.idle()
    assert all(r["on_completion"] == "delete" for r in env.client.of("runs.create"))  # stateless, deleted on completion


async def test_ambient_survives_missing_data(make_env):
    env = await make_env()
    env.data.ok = False
    await env.rt.ambient.set_enabled(True)
    await asyncio.sleep(0.05)
    assert env.rt.ambient.enabled is False and env.rt.ambient.last_error


async def test_the_clock_is_handed_back_at_its_old_speed_when_a_replay_ends(env):
    env.data.events["S01"] = scenario_events("S01", 3, step_s=30)
    env.rt.vclock.set(speed=1)
    await env.rt.replay.start(scenario_id="S01", speed=600)
    assert env.rt.vclock.speed == 600
    await finish(env)
    assert (
        env.rt.vclock.speed == 1 and not env.rt.vclock.paused
    )  # not racing at 600x after the replay (cron would fire every 48 s)
    assert env.rt.vclock.now() >= parse_iso(
        "2026-11-05T10:06:00Z"
    )  # ... but virtual time stays where the replay left it
    env.rt.vclock.set(speed=10, paused=True)
    env.data.events["S01"] = scenario_events("S01", 8, step_s=3600)
    await env.rt.replay.start(scenario_id="S01", speed=60)
    await asyncio.sleep(0.01)
    await env.rt.replay.stop()
    assert env.rt.vclock.speed == 10 and env.rt.vclock.paused  # stop restores what the operator had set


async def test_a_clock_saved_during_a_replay_restarts_at_normal_speed(make_env):
    env = await make_env()
    env.data.events["S01"] = scenario_events("S01", 8, step_s=3600)
    await env.rt.replay.start(scenario_id="S01", speed=600)
    record = env.rt._clock_record()  # noqa: SLF001
    assert record["replay_active"] is True and record["speed"] == 600
    await env.rt.replay.stop()
    await env.rt.db.run(env.rt.db.kv_set, "clock", record)
    await env.rt.stop()
    env2 = (
        await make_env()
    )  # same state DB: replay sessions do not survive a restart, so the clock must not stay at 600x
    assert env2.rt.vclock.speed == 1.0
