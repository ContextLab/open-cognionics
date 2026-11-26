"""The 3-D head, brain and headset in a real Chromium, against the real server.

``scene.html?view=fit|network`` is the development page for the two views. It
shows one view on the sample files and exposes ``window.__scene`` for these
checks (the views, their stage, the headset contacts, counts of what is drawn).
The asserts read scene state: object counts, visibility, material colours,
uniforms and the camera direction. The only waits are for a condition the page
reports, plus the 500 ms between the two frames of the reduced-motion check,
which the check itself is about.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import pytest

from brain_mapper.source import LiveSource

WEB = Path(__file__).resolve().parents[2] / "brain_mapper" / "src" / "brain_mapper" / "web"
SAMPLE_NETWORK = json.loads((WEB / "assets" / "sample-network.json").read_text())
SAMPLE_CONTACTS = json.loads((WEB / "assets" / "sample-contacts.json").read_text())

#: The colours the discs show (the prototype's, also used by the shell tokens).
GOOD, MARGINAL, BAD = "3ed6c8", "f2b33d", "ff5c6c"
#: Camera offset direction of the start view in scene coordinates: from behind and above.
START_DIRECTION = (0.0, 350.0, 197.0)


def open_scene(app_url: str, page, view: str) -> None:
    page.goto(f"{app_url}/static/scene.html?view={view}&test=1")
    page.wait_for_function("window.__scene && window.__scene.ready === true", timeout=30_000)


def start_app(app, pty_device):
    return app(port=pty_device.path)


def angle_between(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot / (na * nb)))))


# -- what the page loads -------------------------------------------------------------


def test_the_scene_loads_nothing_from_another_host(app, pty_device, page):
    a = start_app(app, pty_device)
    host, port = a.host_port
    urls: list[str] = []
    page.on("request", lambda r: urls.append(r.url))
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    for view in ("fit", "network"):
        open_scene(a.url, page, view)
    assert not errors
    assert urls
    for url in urls:
        assert url.startswith((f"http://{host}:{port}/", "data:", "blob:")), url
    names = {u.rsplit("/", 1)[-1] for u in urls}
    assert {"three.module.js", "OrbitControls.js", "meshes.json"} <= names
    assert {"sample-contacts.json", "sample-network.json"} <= names


def test_the_scene_files_name_no_other_host_and_the_vendored_files_are_javascript():
    for path in list((WEB / "js" / "scene3d").glob("*.js")) + [WEB / "scene.html"]:
        text = path.read_text()
        for forbidden in ("googleapis", "gstatic", "cdn.", "https://", "http://"):
            assert forbidden not in text, (path, forbidden)
    version = (WEB / "vendor" / "three" / "VERSION").read_text()
    for rel in ("build/three.module.js", "examples/jsm/controls/OrbitControls.js"):
        file = WEB / "vendor" / "three" / rel
        text = file.read_text()
        assert "export {" in text, rel
        assert not text.lstrip().startswith("<"), rel  # not an HTML error page
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        assert digest in version, rel
    assert "r128" in version
    assert "Copyright" in (WEB / "vendor" / "three" / "LICENSE").read_text()
    assert "REVISION = '128'" in (WEB / "vendor" / "three" / "build" / "three.module.js").read_text()


# -- the headset ---------------------------------------------------------------------


def test_the_headset_has_one_contact_per_eeg_label(app, pty_device, page):
    source = LiveSource(pty_device.path)  # the device's own configuration, read before the app starts
    source.open()
    try:
        labels = list(source.config.labels)
    finally:
        source.stop()
    assert len(labels) == 20
    a = start_app(app, pty_device)
    open_scene(a.url, page, "fit")
    names = page.evaluate("window.__scene.contactNames()")
    assert names == labels
    extras = page.evaluate("window.__scene.fit.headset.extras.map((e) => e.name)")
    assert "A1 clip" in extras
    assert not set(extras) & set(labels)
    assert "A1" not in names


def test_the_headset_is_the_quick_20_shape(app, pty_device, page):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "fit")
    parts = page.evaluate(
        """() => {
          const count = {};
          window.__scene.fit.stage.scene.traverse((o) => {
            const p = o.userData && o.userData.part;
            if (p) count[p] = (count[p] || 0) + 1;
          });
          const fp = window.__scene.fit.headset.contacts.filter((c) => c.name.startsWith('Fp'));
          return {
            count,
            twins: fp.map((c) => {
              const discs = [];
              window.__scene.fit.stage.scene.traverse((o) => {
                if (o.userData && o.userData.part === 'disc' && o.userData.contact === c.name) discs.push(o);
              });
              return [discs.length, discs.length === 2 && discs[0].material === discs[1].material];
            }),
            tipOpacity: (() => { let v = null;
              window.__scene.fit.stage.scene.traverse((o) => { if (o.userData && o.userData.part === 'tip') v = o.material.opacity; });
              return v; })(),
            lobes: window.__scene.meshes.lobes,
          };
        }"""
    )
    count = parts["count"]
    assert count["light"] == 2
    assert count["power-button"] == 1
    assert count["tip"] == 8 * 17  # every contact but the two forehead pads and the right ear
    assert count["disc"] == 22  # 20 contacts, plus the second disc on each forehead pad
    assert count["clip-jaw"] == 4  # two alligator clips, two jaws each
    assert parts["twins"] == [[2, True], [2, True]]
    assert parts["tipOpacity"] == pytest.approx(0.28)
    # the clips hang from the bottom of each earlobe (positions from meshes.json "lobes")
    a1, a2 = parts["lobes"]["A1"]["p"], parts["lobes"]["A2"]["p"]
    contact_a2 = page.evaluate(
        "(() => { const c = window.__scene.fit.headset.contacts.find((x) => x.name === 'A2');"
        " return [c.position.x, c.position.y, c.position.z]; })()"
    )
    assert contact_a2 == pytest.approx(a2, abs=0.01)
    clip_a1 = page.evaluate(
        "(() => { const e = window.__scene.fit.headset.extras.find((x) => x.name === 'A1 clip');"
        " return [e.object.position.x, e.object.position.y, e.object.position.z]; })()"
    )
    assert clip_a1 == pytest.approx(a1, abs=2.5)  # the grey disc sits on the lobe, 1.8 mm out
    hang = page.evaluate(
        """() => Object.fromEntries(['A1', 'A2'].map((n) => {
          const g = window.__scene.fit.headset.extras.find((x) => x.name === n + ' clip hardware').object;
          const ras = [];
          g.children.forEach((o) => { if (o.userData && o.userData.part === 'clip-jaw') {
            const p = window.__scene.fit.headset.group.worldToLocal(o.getWorldPosition(o.position.clone()));
            ras.push([p.x, p.y, p.z]); } });
          return [n, { at: [g.position.x, g.position.y, g.position.z], jaws: ras }];
        }))"""
    )
    for name, lobe in (("A1", a1), ("A2", a2)):
        assert hang[name]["at"] == pytest.approx(lobe, abs=0.01)
        assert len(hang[name]["jaws"]) == 2
        for jaw in hang[name]["jaws"]:
            assert jaw[2] < lobe[2] - 3  # the jaws and the lead point down from the bottom of the lobe
    a1_colour = page.evaluate(
        "window.__scene.fit.headset.extras.find((x) => x.name === 'A1 clip').object.material.color.getHexString()"
    )
    assert a1_colour == "9aa6c4"


def test_contact_states_colour_the_discs(app, pty_device, page):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "fit")
    read = """() => Object.fromEntries(window.__scene.fit.headset.contacts.map(
        (c) => [c.name, c.disc.material.color.getHexString()]))"""
    # the sample states are on after loading: O2 and A2 bad, F8 T4 P8 marginal, the rest good
    before = page.evaluate(read)
    assert before["O2"] == BAD and before["F8"] == MARGINAL and before["F7"] == GOOD
    page.evaluate("window.__scene.fit.setStates({F7: 'bad'})")
    after = page.evaluate(read)
    assert after["F7"] == BAD
    assert {k: v for k, v in after.items() if k != "F7"} == {k: v for k, v in before.items() if k != "F7"}
    page.evaluate("window.__scene.fit.setStates({F7: 'marginal', O2: 'good'})")
    again = page.evaluate(read)
    assert again["F7"] == MARGINAL and again["O2"] == GOOD
    # both discs of a forehead pad share one state
    page.evaluate("window.__scene.fit.setStates({Fp1: 'bad'})")
    both = page.evaluate(
        """() => { const out = [];
          window.__scene.fit.stage.scene.traverse((o) => {
            if (o.userData && o.userData.part === 'disc' && o.userData.contact === 'Fp1') out.push(o.material.color.getHexString());
          }); return out; }"""
    )
    assert both == [BAD, BAD]
    with pytest.raises(Exception, match="state"):
        page.evaluate("window.__scene.fit.setStates({F7: 'purple'})")
    # a label the headset does not have is ignored with a console warning; the others still apply
    warnings: list[str] = []
    page.on("console", lambda m: warnings.append(m.text) if m.type == "warning" else None)
    page.evaluate("window.__scene.fit.setStates({Q9: 'bad', F3: 'bad'})")
    assert page.evaluate(read)["F3"] == BAD
    assert any("Q9" in w for w in warnings), warnings


def test_the_fit_view_starts_from_behind_and_above_zoomed_on_the_electrodes(app, pty_device, page):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "fit")
    state = page.evaluate(
        """() => { const c = window.__scene.fit.stage.camera, t = window.__scene.fit.stage.controls.target;
          return { offset: [c.position.x - t.x, c.position.y - t.y, c.position.z - t.z],
                   target: [t.x, t.y, t.z], fov: c.fov }; }"""
    )
    assert angle_between(state["offset"], START_DIRECTION) < 1.0
    assert state["offset"][1] > 0 and state["offset"][2] > 0  # above, and behind (the nose is toward -z)
    assert math.sqrt(sum(x * x for x in state["offset"])) == pytest.approx(480, abs=8)
    assert state["target"] == pytest.approx([0, 30, 18], abs=0.5)
    assert state["fov"] == 32


def test_clicking_a_status_pill_brings_that_contact_forward(app, pty_device, page):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "fit")
    assert page.evaluate("window.__scene.fit.angleToContact('O2')") > 20  # not already facing it
    page.click('button.pill[data-name="O2"]')
    page.wait_for_function("window.__scene.fit.angleToContact('O2') < 20", timeout=10_000)
    assert page.evaluate("window.__scene.fit.angleToContact('O2')") < 20
    # the camera is nearer the contact than at the start and the target moved toward it
    assert page.evaluate("window.__scene.fit.focused()") == "O2"
    page.wait_for_function("window.__scene.fit.settled()", timeout=10_000)
    assert page.evaluate("window.__scene.fit.stage.camera.position.distanceTo(window.__scene.fit.stage.controls.target)") == pytest.approx(300, abs=3)
    # another contact, by its name on the head
    page.click('.scene3d .labels button:text-is("O1")')
    page.wait_for_function("window.__scene.fit.angleToContact('O1') < 20", timeout=10_000)
    page.evaluate("window.__scene.fit.reset()")
    page.wait_for_function("window.__scene.fit.settled()", timeout=10_000)
    assert page.evaluate("window.__scene.fit.focused()") is None
    offset = page.evaluate(
        """() => { const c = window.__scene.fit.stage.camera, t = window.__scene.fit.stage.controls.target;
          return [c.position.x - t.x, c.position.y - t.y, c.position.z - t.z]; }"""
    )
    assert angle_between(offset, START_DIRECTION) < 1.0


def test_tapping_a_disc_on_the_head_brings_it_forward(app, pty_device, page):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "fit")
    page.evaluate("window.__scene.fit.focus('Pz')")
    page.wait_for_function("window.__scene.fit.settled()", timeout=10_000)
    page.evaluate("window.__scene.fit.reset()")
    page.wait_for_function("window.__scene.fit.settled()", timeout=10_000)
    x, y = page.evaluate("window.__scene.fit.screenPosition('Cz')")
    box = page.locator(".scene3d canvas").bounding_box()
    page.mouse.click(box["x"] + x, box["y"] + y)
    page.wait_for_function("window.__scene.fit.focused() !== null", timeout=5_000)
    assert page.evaluate("window.__scene.fit.focused()") == "Cz"


# -- the brain -----------------------------------------------------------------------

DEPTH = """() => { const b = window.__scene.%s.brain;
  const d = b.depth, m = b.mesh;
  let inScene = false; window.__scene.%s.stage.scene.traverse((o) => { if (o === d) inScene = true; });
  return { inScene, visible: d.visible, colorWrite: d.material.colorWrite, polygonOffset: d.material.polygonOffset,
           factor: d.material.polygonOffsetFactor, units: d.material.polygonOffsetUnits,
           depthOrder: d.renderOrder, brainOrder: m.renderOrder, sameGeometry: d.geometry === m.geometry,
           brainVisible: m.visible, depthWrite: m.material.depthWrite, net: m.material.uniforms.uNet.value }; }"""


def test_the_depth_only_brain_copy_is_visible_in_the_network_view_and_hidden_in_the_fit_view(
    app, pty_device, page
):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "network")
    net = page.evaluate(DEPTH % ("network", "network"))
    assert net["inScene"] and net["visible"] is True
    assert net["colorWrite"] is False and net["polygonOffset"] is True
    assert (net["factor"], net["units"]) == (1, 1)
    assert net["depthOrder"] < net["brainOrder"] and net["brainOrder"] - net["depthOrder"] <= 0.5
    assert net["sameGeometry"] and net["brainVisible"] and net["depthWrite"] is False
    assert net["net"] == pytest.approx(1)
    open_scene(a.url, page, "fit")
    fit = page.evaluate(DEPTH % ("fit", "fit"))
    assert fit["inScene"] and fit["visible"] is False
    assert fit["net"] == 0
    # the switch is the network mix above one half
    page.evaluate("window.__scene.fit.brain.setMix(0.6)")
    assert page.evaluate("window.__scene.fit.brain.depth.visible") is True
    page.evaluate("window.__scene.fit.brain.setMix(0.5)")
    assert page.evaluate("window.__scene.fit.brain.depth.visible") is False


def test_the_head_is_a_hollow_shell_and_the_brain_is_inside_it(app, pty_device, page):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "fit")
    info = page.evaluate(
        """() => { const s = window.__scene, h = s.fit.head.mesh, b = s.fit.brain.mesh;
          h.geometry.computeBoundingBox(); b.geometry.computeBoundingBox();
          const hb = h.geometry.boundingBox, bb = b.geometry.boundingBox;
          return { headFaces: h.geometry.index.count / 3, brainFaces: b.geometry.index.count / 3,
                   headBox: [hb.min.toArray(), hb.max.toArray()], brainBox: [bb.min.toArray(), bb.max.toArray()],
                   headFade: h.material.uniforms.uFade.value, headSide: h.material.side }; }"""
    )
    for axis in range(3):  # the brain lies inside the head's box (the head also holds the neck)
        assert info["headBox"][0][axis] < info["brainBox"][0][axis]
        assert info["headBox"][1][axis] > info["brainBox"][1][axis]
    assert info["headFaces"] == 73008 / 6  # the head of meshes.json: inner surfaces already removed
    assert info["headFade"] == 1
    assert info["headSide"] == 0  # FrontSide


# -- the network view ----------------------------------------------------------------


def test_the_network_view_draws_the_given_nodes_and_links(app, pty_device, page):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "network")
    threshold = page.evaluate("window.__scene.network.displayThreshold")
    assert threshold == 0.405
    expected_links = sum(1 for _, _, w in SAMPLE_NETWORK["edges"] if w >= threshold)
    assert expected_links < len(SAMPLE_NETWORK["edges"])  # the file holds weaker links too
    page.wait_for_function("window.__scene.network.settled()", timeout=10_000)
    counts = page.evaluate("window.__scene.network.counts()")
    assert counts == {"nodes": len(SAMPLE_NETWORK["nodes"]), "links": expected_links}
    assert counts["links"] == 64
    # a different network replaces the first
    page.evaluate(
        """() => window.__scene.network.setNetwork({
             nodes: [[-40, 0, 30], [40, 0, 30], [0, 30, 40], [0, -40, 20]],
             communities: [0, 0, 1, 1],
             edges: [[0, 1, 0.9], [0, 2, 0.5], [2, 3, 0.7], [1, 3, 0.01]] })"""
    )
    page.wait_for_function("window.__scene.network.settled()", timeout=10_000)
    assert page.evaluate("window.__scene.network.counts()") == {"nodes": 4, "links": 3}


def test_selecting_a_node_keeps_only_its_links_lit(app, pty_device, page):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "network")
    page.wait_for_function("window.__scene.network.settled()", timeout=10_000)
    degree = sum(1 for i, j, w in SAMPLE_NETWORK["edges"] if w >= 0.405 and 0 in (i, j))
    page.evaluate("window.__scene.network.select(0)")
    page.wait_for_function("window.__scene.network.settled()", timeout=10_000)
    lit = page.evaluate("window.__scene.network.litLinks()")
    assert lit == degree
    assert page.evaluate("window.__scene.network.selected()") == 0
    page.evaluate("window.__scene.network.select(-1)")
    page.wait_for_function("window.__scene.network.settled()", timeout=10_000)
    assert page.evaluate("window.__scene.network.litLinks()") == 64


def test_the_network_view_fades_the_head_and_has_no_headset(app, pty_device, page):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "network")
    info = page.evaluate(
        """() => { const n = window.__scene.network;
          return { headFade: n.head.mesh.material.uFade !== undefined ? 0 : n.head.mesh.material.uniforms.uFade.value,
                   headset: !!n.headset, brainNet: n.brain.mesh.material.uniforms.uNet.value }; }"""
    )
    assert info["headFade"] == pytest.approx(1 - 0.82)
    assert info["headset"] is False
    assert info["brainNet"] == pytest.approx(1)


def test_reduced_motion_stops_the_pulses(app, pty_device, browser):
    a = start_app(app, pty_device)
    read = "window.__scene.network.pulseUniforms()"

    def two_frames(context):
        page = context.new_page()
        open_scene(a.url, page, "network")
        page.wait_for_function("window.__scene.network.settled()", timeout=10_000)
        first = page.evaluate(read)
        page.evaluate("new Promise((r) => setTimeout(r, 500))")
        second = page.evaluate(read)
        return first, second, page.evaluate("window.__scene.network.stage.reducedMotion")

    still = browser.new_context(reduced_motion="reduce")
    try:
        first, second, reduced = two_frames(still)
    finally:
        still.close()
    assert reduced is True
    assert len(first) == 64 and first == second
    moving = browser.new_context(reduced_motion="no-preference")
    try:
        first, second, reduced = two_frames(moving)
    finally:
        moving.close()
    assert reduced is False
    assert len(first) == 64 and first != second


# -- the page ------------------------------------------------------------------------


def test_both_views_say_sample_data_when_fed_from_the_sample_files(app, pty_device, page):
    a = start_app(app, pty_device)
    for view in ("fit", "network"):
        open_scene(a.url, page, view)
        badge = page.locator(".scene3d .sample-badge")
        assert badge.count() == 1
        assert badge.is_visible()
        assert "Sample data" in badge.inner_text()
        page.evaluate(f"window.__scene.{view}.setSample(false)")
        assert not page.locator(".scene3d .sample-badge").is_visible()


@pytest.mark.parametrize("view", ["fit", "network"])
def test_the_scene_says_so_when_webgl_is_unavailable(app, pty_device, browser, view):
    a = start_app(app, pty_device)
    context = browser.new_context()
    try:
        page = context.new_page()
        # a browser with WebGL switched off: asking a canvas for a 3-D context gives nothing
        page.add_init_script(
            """const real = HTMLCanvasElement.prototype.getContext;
               HTMLCanvasElement.prototype.getContext = function (kind, ...rest) {
                 return /webgl/.test(kind) ? null : real.call(this, kind, ...rest);
               };"""
        )
        page.goto(f"{a.url}/static/scene.html?view={view}&test=1")
        page.wait_for_selector(".scene3d .scene-message", timeout=15_000)
        assert "WebGL" in page.inner_text(".scene3d .scene-message")
        assert page.locator(".scene3d canvas").count() == 0
        # the page does not claim to be ready: the status says the browser cannot draw it
        page.wait_for_function("window.__scene && window.__scene.status !== 'loading'")
        assert page.evaluate("window.__scene.status") == "unsupported"
        assert page.evaluate("window.__scene.ready") is False
    finally:
        context.close()


@pytest.mark.parametrize("view", ["fit", "network"])
def test_disposing_a_view_removes_its_canvas_stops_its_frames_and_frees_the_gl_context(
    app, pty_device, page, view
):
    a = start_app(app, pty_device)
    open_scene(a.url, page, view)
    assert page.locator(".scene3d canvas").count() == 1
    page.evaluate(f"window.__gl = window.__scene.{view}.stage.renderer.getContext()")
    assert page.evaluate("window.__gl.isContextLost()") is False
    page.evaluate(f"window.__scene.{view}.dispose()")
    assert page.locator(".scene3d canvas").count() == 0
    assert page.locator(".scene3d .labels button").count() == 0
    assert page.locator(".scene3d .hint, .scene3d .sample-badge").count() == 0
    page.wait_for_function("window.__gl.isContextLost()", timeout=5_000)
    frames = page.evaluate(f"window.__scene.{view}.stage.frames()")
    page.evaluate("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    assert page.evaluate(f"window.__scene.{view}.stage.frames()") == frames


def test_the_test_hook_exists_only_when_the_page_is_opened_with_the_test_flag(app, pty_device, page):
    a = start_app(app, pty_device)
    page.goto(f"{a.url}/static/scene.html?view=fit")
    page.wait_for_selector(".scene3d canvas", timeout=30_000)
    assert page.evaluate("typeof window.__scene") == "undefined"
    exposed = page.evaluate("window.__scene === undefined")
    assert exposed is True


def test_a_media_query_change_to_reduced_motion_stills_the_pulses_and_turns_damping_off(
    app, pty_device, page
):
    a = start_app(app, pty_device)
    page.emulate_media(reduced_motion="no-preference")
    open_scene(a.url, page, "network")
    page.wait_for_function("window.__scene.network.settled()", timeout=10_000)
    stage = "window.__scene.network.stage"
    assert page.evaluate(f"{stage}.reducedMotion") is False
    assert page.evaluate(f"{stage}.controls.enableDamping") is True
    first = page.evaluate("window.__scene.network.pulseUniforms()")
    page.evaluate("new Promise((r) => setTimeout(r, 300))")
    assert page.evaluate("window.__scene.network.pulseUniforms()") != first  # moving
    page.emulate_media(reduced_motion="reduce")
    page.wait_for_function(f"{stage}.reducedMotion === true", timeout=5_000)
    assert page.evaluate(f"{stage}.controls.enableDamping") is False
    page.evaluate("new Promise((r) => setTimeout(r, 200))")
    still = page.evaluate("window.__scene.network.pulseUniforms()")
    page.evaluate("new Promise((r) => setTimeout(r, 500))")
    assert page.evaluate("window.__scene.network.pulseUniforms()") == still
    page.emulate_media(reduced_motion="no-preference")
    page.wait_for_function(f"{stage}.reducedMotion === false", timeout=5_000)
    assert page.evaluate(f"{stage}.controls.enableDamping") is True


@pytest.mark.parametrize("size", [(1280, 720), (390, 800)], ids=["default", "narrow"])
def test_contact_names_stay_clear_of_the_hint_line(app, pty_device, browser, size):
    a = start_app(app, pty_device)
    context = browser.new_context(viewport={"width": size[0], "height": size[1]})
    try:
        page = context.new_page()
        open_scene(a.url, page, "fit")
        page.wait_for_function("window.__scene.fit.stage.frames() > 5")
        check_clear_of_hint(page, size)
    finally:
        context.close()


def check_clear_of_hint(page, size):
    stage_width = page.evaluate("document.getElementById('stage').getBoundingClientRect().width")
    assert stage_width >= min(size[0] * 0.8, 600), "the stage must use the width of the screen"
    line = page.evaluate("parseFloat(getComputedStyle(document.querySelector('.scene3d .hint')).lineHeight) || 20")
    hint_height = page.evaluate("document.querySelector('.scene3d .hint').getBoundingClientRect().height")
    if size[0] < 500:
        assert hint_height > 1.5 * line, "at this width the hint must wrap to more than one line"
    check = """() => {
      const hint = document.querySelector('.scene3d .hint').getBoundingClientRect();
      const clash = [];
      document.querySelectorAll('.scene3d .labels button').forEach((b) => {
        if (parseFloat(b.style.opacity || '0') < 0.05) return;
        const r = b.getBoundingClientRect();
        if (r.bottom > hint.top && r.top < hint.bottom && r.right > hint.left && r.left < hint.right) clash.push(b.textContent);
      });
      return clash;
    }"""
    assert page.evaluate(check) == []
    # turn the head through many views so that names pass through the foot of the stage
    sweep = """async ([azimuth, elevation]) => {
      const { camera, controls } = window.__scene.fit.stage;
      const off = camera.position.clone().sub(controls.target);
      const r = off.length();
      const a = (azimuth * Math.PI) / 180;
      const e = (elevation * Math.PI) / 180;
      camera.position.set(
        controls.target.x + r * Math.sin(a) * Math.cos(e),
        controls.target.y + r * Math.sin(e),
        controls.target.z + r * Math.cos(a) * Math.cos(e),
      );
      controls.update();
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      return (%s)();
    }""" % check
    for elevation in (-35, 0, 20, 45, 75):
        for azimuth in range(0, 360, 30):
            assert page.evaluate(sweep, [azimuth, elevation]) == [], (azimuth, elevation)
    page.evaluate("window.__scene.fit.focus('O2')")
    page.wait_for_function("window.__scene.fit.settled()", timeout=10_000)
    page.wait_for_function("window.__scene.fit.stage.frames() > 0")
    assert page.evaluate(check) == []
    # the labels that are clear of the hint are still shown
    shown = page.evaluate(
        "[...document.querySelectorAll('.scene3d .labels button')].filter((b) => parseFloat(b.style.opacity || '0') > 0.5).length"
    )
    assert shown >= 3


# -- when something cannot be loaded -------------------------------------------------


@pytest.mark.parametrize(
    "view, asset, how, wanted",
    [
        ("fit", "meshes.json", "abort", "meshes.json"),
        ("fit", "sample-contacts.json", "abort", "sample-contacts.json"),
        ("network", "sample-network.json", "404", "sample-network.json"),
        ("network", "meshes.json", "404", "meshes.json"),
        ("fit", "three.module.js", "abort", "3-D code"),
    ],
)
def test_a_failed_load_shows_a_plain_message_and_no_uncaught_exception(
    app, pty_device, page, view, asset, how, wanted
):
    a = start_app(app, pty_device)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if how == "abort":
        page.route(f"**/{asset}", lambda route: route.abort())
    else:
        page.route(f"**/{asset}", lambda route: route.fulfill(status=404, body="not here"))
    page.goto(f"{a.url}/static/scene.html?view={view}&test=1")
    page.wait_for_function("window.__scene && window.__scene.status === 'failed'", timeout=30_000)
    message = page.inner_text("#stage .scene-message")
    assert wanted in message and "could not" in message.lower()
    assert page.evaluate("window.__scene.ready") is False
    assert page.evaluate("window.__scene.error").startswith("Could not load the 3-D view")
    assert page.locator("#stage canvas").count() == 0
    assert errors == []


def test_a_sample_file_with_an_unknown_contact_label_still_shows_the_view(app, pty_device, page):
    a = start_app(app, pty_device)
    errors: list[str] = []
    warnings: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: warnings.append(m.text) if m.type == "warning" else None)

    def add_label(route):  # the real file, with one label the headset does not have
        response = route.fetch()
        body = response.json()
        body["Q9"] = "bad"
        route.fulfill(response=response, json=body)

    page.route("**/sample-contacts.json", add_label)
    open_scene(a.url, page, "fit")
    assert page.evaluate("window.__scene.status") == "ready"
    assert any("Q9" in w for w in warnings), warnings
    assert page.locator(".scene3d canvas").count() == 1
    assert page.evaluate("window.__scene.fit.headset.state('O2')") == "bad"
    assert errors == []


# -- the code ------------------------------------------------------------------------


#: Geometry constructors that files other than headset-geometry.js may call, with the reason:
#: the decoded head and brain meshes, and the node sphere and fibre bundle of the network view.
OTHER_GEOMETRY = {
    "mesh-data.js": {"BufferGeometry"},
    "network-parts.js": {"BufferGeometry", "SphereGeometry"},
}


def test_headset_shapes_are_built_in_one_module_only():
    js = WEB / "js"
    geometry = (js / "scene3d" / "headset-geometry.js").read_text()
    assert "Geometry(" in geometry
    assert "to be replaced" in geometry
    files = sorted(p for p in js.rglob("*.js") if p.name != "headset-geometry.js")
    assert len(files) >= 15
    for path in files:
        text = path.read_text()
        # every three.js geometry constructor in the file must be one it is allowed to use
        used = set(re.findall(r"new\s+(?:THREE\.)?(\w*Geometry)\s*\(", text))
        assert used <= OTHER_GEOMETRY.get(path.name, set()), (path, used)
        # no headset part is built or named outside headset-geometry.js
        assert not re.search(r"\b(earClip|ribbon|hubC|alligator|BoxGeometry|CylinderGeometry|TubeGeometry)\b", text), path
    for name in ("headset.js", "fit-view.js", "network-view.js"):
        text = (js / "scene3d" / name).read_text()
        assert "Geometry(" not in text, name
        assert "BufferGeometry" not in text, name
    # headset-geometry.js is imported by headset.js alone
    for path in files:
        if path.name != "headset.js":
            assert "headset-geometry" not in path.read_text(), path


def test_the_sample_files_are_what_the_script_writes(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "brain_mapper_sample_data.py"
    out = tmp_path / "samples"
    out.mkdir()
    subprocess.run([sys.executable, "-I", str(script), str(out)], check=True, capture_output=True)
    for name in ("sample-contacts.json", "sample-network.json"):
        assert (out / name).read_bytes() == (WEB / "assets" / name).read_bytes(), name


def test_the_headset_geometry_header_states_the_contract():
    text = (WEB / "js" / "scene3d" / "headset-geometry.js").read_text()
    head = text[: text.index("export")]
    assert "contacts" in head and "extras" in head and "glass" in head
    assert "photograph" in head


def test_the_credits_are_one_factual_line_each():
    credits = (WEB / "assets" / "CREDITS.md").read_text()
    for needle in ("three.js", "MIT", "Lee Perry-Smith", "CC BY 3.0", "FreeSurfer fsaverage", "Inter", "Bricolage"):
        assert needle in credits, needle


def test_the_start_screen_shows_the_credit_line(app, pty_device, page):
    a = start_app(app, pty_device)
    page.goto(a.url)
    page.wait_for_selector('#screen[data-screen="start"]', timeout=20_000)
    text = page.inner_text(".credit")
    assert "Lee Perry-Smith" in text and "CC BY 3.0" in text and "FreeSurfer" in text


def test_a_hidden_hint_does_not_hide_the_contact_names(app, pty_device, page):
    a = start_app(app, pty_device)
    open_scene(a.url, page, "fit")
    page.wait_for_function("window.__scene.fit.stage.frames() > 5")
    shown = "[...document.querySelectorAll('.scene3d .labels button')].filter((b) => parseFloat(b.style.opacity || '0') > 0.5).length"
    before = page.evaluate(shown)
    page.evaluate("document.querySelector('.scene3d .hint').style.display = 'none'")
    frames = page.evaluate("window.__scene.fit.stage.frames()")
    page.wait_for_function(f"window.__scene.fit.stage.frames() > {frames + 3}")
    assert page.evaluate(shown) >= max(3, before)
    # and the hint comes back: names keep clear of it again
    page.evaluate("document.querySelector('.scene3d .hint').style.display = ''")
    frames = page.evaluate("window.__scene.fit.stage.frames()")
    page.wait_for_function(f"window.__scene.fit.stage.frames() > {frames + 3}")
    assert page.evaluate(
        """() => { const hint = document.querySelector('.scene3d .hint').getBoundingClientRect();
          return [...document.querySelectorAll('.scene3d .labels button')].filter((b) => {
            if (parseFloat(b.style.opacity || '0') < 0.05) return false;
            const r = b.getBoundingClientRect(); return r.bottom > hint.top && r.top < hint.bottom; }).length; }"""
    ) == 0


def test_the_hint_is_measured_on_resize_not_every_frame(app, pty_device, browser):
    a = start_app(app, pty_device)
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    try:
        page = context.new_page()
        open_scene(a.url, page, "fit")
        page.wait_for_function("window.__scene.fit.stage.frames() > 5")
        page.evaluate("document.fonts.ready.then(() => true)")  # a font load resizes the labels
        frames = page.evaluate("window.__scene.fit.stage.frames()")
        page.wait_for_function(f"window.__scene.fit.stage.frames() > {frames + 5}")
        # count the layout reads of the hint's box over many frames
        page.evaluate(
            """() => { const hint = document.querySelector('.scene3d .hint');
              window.__reads = 0; const real = hint.getBoundingClientRect.bind(hint);
              hint.getBoundingClientRect = () => { window.__reads++; return real(); }; }"""
        )
        frames = page.evaluate("window.__scene.fit.stage.frames()")
        page.wait_for_function(f"window.__scene.fit.stage.frames() > {frames + 30}")
        assert page.evaluate("window.__reads") == 0
        # a resize is still noticed: the page narrows and the names stay clear of the longer hint
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_function("window.__reads > 0")
        check_clear_of_hint(page, (390, 800))
    finally:
        context.close()
