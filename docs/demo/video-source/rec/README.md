# rec: offscreen WKWebView -> H.264 mp4 recorder

Build: `swiftc -O -swift-version 5 -suppress-warnings rec.swift -o rec`   (binary already built; `vtool` = mp4 inspector, built from vtool.swift)

The page is rendered in a WKWebView in an NSWindow at (-5000,-5000), CSS viewport 1280x720 (`innerWidth` 1280, `devicePixelRatio` 1.5: the view is 960x540 pt with
pageZoom 0.75 on the 2x backing, so cacheDisplay returns exactly 1920x1080 px, rendered natively at the output scale = crisp and ~40% cheaper than grabbing 2560x1440),
the two KVC settings from PRODUCTION.md keep it "visible" (document.hidden=false; policy `.accessory`, rec flips to `.regular` automatically if the page is ever hidden).
Frames are grabbed on a 30 fps wall-clock timer (cacheDisplay costs ~25 ms on light pages, 45-50 ms on the heavy casting/writers pages, so effective UNIQUE frames are
~28/s light, ~20/s heavy; late ticks REPEAT the last frame so pts == real time and video duration == real duration, +-1 frame), written with AVAssetWriter (H.264 High, ~9 Mbps cap, BT.709, no B-frames, keyframe every 1 s, faststart, no audio).
SIGINT/SIGTERM/--max-seconds finalise the file cleanly. Nothing is persisted (non-persistent data store) unless --profile is given.

## CLI
```
rec --url URL --out file.mp4 [flags]
  --css WxH            CSS viewport (default 1280x720)
  --out-size WxH       video size (default 1920x1080)
  --dpr X              device pixel ratio the page renders at (default out width / css width = 1.5)
  --fps N              default 30
  --max-seconds S      hard stop (default 300)  <-- raise it for long runs
  --tail S             extra seconds recorded after the director ends (default 2)
  --director file.js   run as the body of an async function in the page after load (see below)
  --pre-js file.js     injected at document start (repeatable, after the helper lib)
  --lib file.js        helper library (default: director_lib.js next to the binary)
  --profile NAME       persistent WKWebsiteDataStore(forIdentifier:) keyed by NAME (shares localStorage between rec runs)
  --shot out.png --after S    PNG (out-size) S seconds after load, then exit (director runs meanwhile if given)
  --eval 'js'  [--after S]    evaluate js (expression or async body), print the result (JSON/str), exit
  --dump-text  [--after S]    print document.body.innerText, exit
  --policy auto|accessory|regular    --bitrate N    --settle S (default 0.4 s after load)   --console-all   --follow-popups   --fragment   --diag
```
Outputs: `<out>` (mp4), `<out>.markers.json` (array of `{name,t,data}`, t = seconds since recording start; rewritten on each mark so it survives a crash),
`<out>.meta.json` (duration, frames, repeated frames, avg grab ms, console error count, stop reason).
stderr: `[mark t] name`, `[log t] ...` (rec.log + director helper logs), `[console.error] ...` (page console errors/warns, uncaught errors, unhandled rejections), page alert/confirm (auto-accepted), popups.
Recording starts when the page has loaded (+settle) and the director starts at t=0. It ends at director end + tail, --max-seconds, or SIGINT.
Run long recordings in the background: `rec ... 2> rec.log &`; stop early with `kill -INT <pid>`.

## Director API (director_lib.js, all globals; `rec` is the native bridge)
- `rec.mark(name, data)`, `rec.log(...)`, `rec.done()`, `rec.t()` (seconds since recording start), `rec.cursor = false` removes the fake cursor.
- `sleep(ms)`, `frame()`, `q(sel)`, `qa(sel)`, `isVisible(el)`
- `byText(textOrRegExp, {tag, exact, visible=true, nth, last})` deepest visible element whose text (or aria-label/title/placeholder/alt) matches; `byTextAll(...)`
- `click(elOrTextOrRegExp|"css:sel"|"#id", {timeout, moveMs, after, force})` waits for it to appear and be enabled, smooth-scrolls it into view, glides the cursor (~0.5-0.9 s eased bow path),
  ripple + press, dispatches pointerover/mouseover/pointermove/pointerdown/mousedown/focus/pointerup/mouseup/click. Returns the element or null (logs, never throws).
- `hover(target)`, `typeInto(elOrText, text, {perChar:true, delay:65, click:true})` (placeholder text works as target: `typeInto('Seed (optional)','7')`), `pressKey('2'|'Enter'|'Escape'|'ArrowDown', {target})`
- `waitText(textOrRegExp, timeout=20000, {exact,tag})`, `waitGone(text, timeout)`, `waitFor(fn, timeout=15000, label)`, `waitEl(target)`, `waitUrl(fragment)`; all return truthy or null on timeout (log WARN), never hang; pass `{throw:true}` as 3rd/4th arg to throw.
- `hasText(text)`, `bodyText()`, `scrollToEl(el,{block:'center'|'start'})`, `smoothScrollBy(container, dy, ms)`, `scrollParent(el)`, `cursor.move(x,y)`, `cursor.hide()/show()/remove()`, `safe(label, async fn)` (log + swallow errors)
- Director errors are logged (`DIRECTOR ERROR`) and end the recording (after tail); the fake cursor lives on <html>, so SPA re-renders do not remove it.

## Helpers
`rec/vtool info f.mp4 | frame f.mp4 T out.png [width] | dups f.mp4 [s e] | stats f.mp4 T... | timeline f.mp4 [step]`

Examples: `rec --url http://127.0.0.1:2027/app/ --out V/raw/x.mp4 --director V/rec/t_flow.js --tail 1.5 --max-seconds 40`
          `rec --url http://127.0.0.1:2027/app/ --eval 'document.title'`      `rec --url ... --shot /tmp/a.png --after 2`

## LangGraph Studio (https://smith.langchain.com/studio) in the recorder
Studio renders fine, BUT an https page cannot fetch http://127.0.0.1 in WebKit (mixed content; `--no-web-security` does not help), so Studio shows "Failed to initialize Studio / Load failed".
Fix used: `python3 V/rec/tlsproxy.py 2028 2027` (local-only HTTPS reverse proxy with a self-signed cert, streams SSE) and point Studio at it:
`https://smith.langchain.com/studio/thread?baseUrl=https%3A%2F%2F127.0.0.1%3A2028&render=interact&mode=graph&assistantId=<uuid>[&threadId=<id>]`
rec accepts the self-signed cert for 127.0.0.1/localhost automatically (server-trust challenge). Nothing is signed in; no credentials are used.

## Director scripts / outputs (agent R)
- `director_game.js` : the full real-game film (seed 7, Farce, Madame's pick, middle draft, candle rehearsal, 1x show, Prompter option 2, reviews, gazette, almanac, Prompt Book links) -> `V/raw/game.mp4` + `V/raw/footage.json` (built by `make_footage_json.py raw/game.mp4`).
- `director_studio_thread.js`, `director_studio_live.js` (casting_call), `director_studio_live_perf.js` (performance) -> `V/raw/studio_*.mp4` (need `tlsproxy.py` running; use `--css 1440x810`).
- `t_flow.js`, `t_fps.js`: small test directors.
