// rec: offscreen WKWebView -> H.264 .mp4 recorder (see README.md next to this file)
// build: swiftc -O -swift-version 5 rec.swift -o rec
import AppKit
import WebKit
import AVFoundation
import CoreVideo
import CryptoKit
import ImageIO
import UniformTypeIdentifiers

setvbuf(stdout, nil, _IONBF, 0)
setvbuf(stderr, nil, _IONBF, 0)

func err(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

// MARK: options
struct Opts {
    var url = ""
    var out = ""
    var cssW = 1280, cssH = 720
    var outW = 1920, outH = 1080
    var fps = 30
    var maxSeconds = 300.0
    var tail = 2.0
    var director: String? = nil
    var preJS: [String] = []
    var lib: String? = nil
    var shot: String? = nil
    var after = 1.0
    var eval: String? = nil
    var dumpText = false
    var profile: String? = nil
    var policy = "auto"   // auto | accessory | regular
    var bitrate = 9_000_000
    var consoleAll = false
    var settle = 0.4
    var diag = false
    var userAgentSuffix: String? = nil
    var followPopups = false
    var fragment = false
    var noWebSecurity = false
    var dpr = 0.0
    var insecureKeys: [String] = []
}

func usage() -> Never {
    err("""
    usage: rec --url URL [--out file.mp4] [options]
      --css WxH            CSS viewport (default 1280x720; frames are grabbed at the display scale, 2x)
      --out-size WxH       video size (default 1920x1080)
      --fps N              constant frame rate (default 30)
      --max-seconds S      hard stop (default 300)
      --tail S             seconds recorded after the director ends (default 2)
      --director file.js   async function body run in the page after load (helpers from director_lib.js; global `rec`)
      --pre-js file.js     injected at document start (repeatable)
      --lib file.js        helper library (default: director_lib.js next to this binary)
      --profile NAME       persistent website data store keyed by NAME (shared localStorage); default is non-persistent
      --shot out.png --after S   single PNG (out-size) S seconds after load, then exit (director runs meanwhile if given)
      --eval 'js'          run js in the page (expression or async body), print result, exit  (--after S waits first)
      --dump-text          print document.body.innerText and exit
      --policy auto|accessory|regular   activation policy (auto: accessory, flips to regular if the page is hidden)
      --bitrate N          bits/s (default 9000000)
      --console-all        also print console.log/info/debug from the page
      --settle S           pause after load before recording starts (default 0.4)
      --dpr X              device pixel ratio the page renders at (default = out width / css width = 1.5; the view is sized css*dpr/backingScale points so grabs are already output-sized)
      --no-web-security    disable WebKit web security (mixed content / CORS) so https pages can reach http://127.0.0.1
      --follow-popups      load window.open / target=_blank in the same view
      --diag               print visibility / fps diagnostics
    outputs: <out>, <out>.markers.json (array of {name,t,data}), <out>.meta.json
    """)
    exit(2)
}

func parseWH(_ s: String) -> (Int, Int)? {
    let p = s.lowercased().split(separator: "x").compactMap { Int($0) }
    return p.count == 2 ? (p[0], p[1]) : nil
}

var opts = Opts()
do {
    var a = Array(CommandLine.arguments.dropFirst())
    func next(_ flag: String) -> String { guard !a.isEmpty else { err("missing value for \(flag)"); usage() }; return a.removeFirst() }
    while !a.isEmpty {
        let f = a.removeFirst()
        switch f {
        case "--url": opts.url = next(f)
        case "--out": opts.out = next(f)
        case "--css": guard let w = parseWH(next(f)) else { usage() }; (opts.cssW, opts.cssH) = w
        case "--out-size": guard let w = parseWH(next(f)) else { usage() }; (opts.outW, opts.outH) = w
        case "--fps": opts.fps = Int(next(f)) ?? 30
        case "--max-seconds": opts.maxSeconds = Double(next(f)) ?? 300
        case "--tail": opts.tail = Double(next(f)) ?? 2
        case "--director": opts.director = next(f)
        case "--pre-js": opts.preJS.append(next(f))
        case "--lib": opts.lib = next(f)
        case "--shot": opts.shot = next(f)
        case "--after": opts.after = Double(next(f)) ?? 1
        case "--eval": opts.eval = next(f)
        case "--dump-text": opts.dumpText = true
        case "--profile": opts.profile = next(f)
        case "--policy": opts.policy = next(f)
        case "--bitrate": opts.bitrate = Int(next(f)) ?? 9_000_000
        case "--console-all": opts.consoleAll = true
        case "--settle": opts.settle = Double(next(f)) ?? 0.4
        case "--diag": opts.diag = true
        case "--follow-popups": opts.followPopups = true
        case "--fragment": opts.fragment = true
        case "--no-web-security": opts.noWebSecurity = true
        case "--dpr": opts.dpr = Double(next(f)) ?? 0
        case "--pref-false": opts.insecureKeys.append(next(f))
        case "-h", "--help": usage()
        default: err("unknown flag \(f)"); usage()
        }
    }
    if opts.url.isEmpty { usage() }
    let isInspect = opts.shot != nil || opts.eval != nil || opts.dumpText
    if !isInspect && opts.out.isEmpty { err("--out is required when recording"); usage() }
}

func readFile(_ p: String) -> String {
    guard let s = try? String(contentsOfFile: p, encoding: .utf8) else { err("cannot read \(p)"); exit(2) }
    return s
}

let exeDir: String = {
    var p = CommandLine.arguments[0]
    if !p.contains("/") { p = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map { "\($0)/\(p)" }.first { FileManager.default.isExecutableFile(atPath: $0) } ?? p }
    return ((URL(fileURLWithPath: p).resolvingSymlinksInPath().path) as NSString).deletingLastPathComponent
}()

// JS bridge injected at document start, before the helper library.
let bridgeJS = """
(function(){
  if (window.rec && window.rec.__bridge) return;
  var post = function(o){ try { window.webkit.messageHandlers.rec.postMessage(o); } catch(e) {} };
  var fmt = function(a){ return Array.prototype.map.call(a, function(x){ if (typeof x === 'string') return x; try { return (x && x.stack) ? x.stack : JSON.stringify(x); } catch(e) { return String(x); } }).join(' '); };
  var rec = {
    __bridge: true, cursor: true, _t0: Date.now(),
    mark: function(name, data){ var d = null; try { d = (data === undefined) ? null : JSON.parse(JSON.stringify(data)); } catch(e) { d = String(data); } post({type:'mark', name:String(name), data:d}); },
    log: function(){ post({type:'log', msg: fmt(arguments)}); },
    done: function(){ post({type:'done'}); },
    t: function(){ return (Date.now() - rec._t0) / 1000; }
  };
  window.rec = rec;
  ['error','warn'].concat(window.__recConsoleAll ? ['log','info','debug'] : []).forEach(function(l){
    var orig = console[l] ? console[l].bind(console) : function(){};
    console[l] = function(){ try { post({type:'console', level:l, msg: fmt(arguments)}); } catch(e) {} orig.apply(null, arguments); };
  });
  window.addEventListener('error', function(e){ post({type:'console', level:'error', msg:'uncaught: ' + e.message + ' @' + (e.filename||'') + ':' + e.lineno}); });
  window.addEventListener('unhandledrejection', function(e){ post({type:'console', level:'error', msg:'unhandled rejection: ' + fmt([e.reason])}); });
})();
"""

// MARK: recorder
final class VideoSink {
    let W: Int, H: Int, fps: Int
    let writer: AVAssetWriter
    let input: AVAssetWriterInput
    let adaptor: AVAssetWriterInputPixelBufferAdaptor
    var pool: CVPixelBufferPool?
    let q = DispatchQueue(label: "rec.encode")
    var lastIdx = -1               // last pts index appended (touched only on q)
    var lastBuf: CVPixelBuffer?    // touched only on q
    var lastIdxMain = -1           // main-thread mirror of the last scheduled index
    var written = 0, repeated = 0, dropped = 0, procMs = 0.0, procN = 0
    let cs = CGColorSpace(name: CGColorSpace.sRGB)!

    init(url: URL, W: Int, H: Int, fps: Int, bitrate: Int, fragment: Bool) throws {
        self.W = W; self.H = H; self.fps = fps
        try? FileManager.default.removeItem(at: url)
        writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let settings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: W, AVVideoHeightKey: H,
            AVVideoColorPropertiesKey: [
                AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
                AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
                AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2,
            ],
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: bitrate,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
                AVVideoExpectedSourceFrameRateKey: fps,
                AVVideoMaxKeyFrameIntervalKey: fps,
                AVVideoAllowFrameReorderingKey: false,
            ] as [String: Any],
        ]
        input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        input.expectsMediaDataInRealTime = true
        adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: W, kCVPixelBufferHeightKey as String: H,
            kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
        ])
        writer.add(input)
        if fragment { writer.movieFragmentInterval = CMTime(seconds: 4, preferredTimescale: 600) } else { writer.shouldOptimizeForNetworkUse = true }
        guard writer.startWriting() else { throw writer.error ?? NSError(domain: "rec", code: 1) }
        writer.startSession(atSourceTime: .zero)
        CVPixelBufferPoolCreate(nil, nil, [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: W, kCVPixelBufferHeightKey as String: H,
            kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
            kCVPixelBufferIOSurfacePropertiesKey as String: [:],
        ] as CFDictionary, &pool)
    }

    func makeBuffer(from img: CGImage) -> CVPixelBuffer? {
        var pb: CVPixelBuffer?
        guard let pool = pool, CVPixelBufferPoolCreatePixelBuffer(nil, pool, &pb) == kCVReturnSuccess, let buf = pb else { return nil }
        CVBufferSetAttachment(buf, kCVImageBufferColorPrimariesKey, kCVImageBufferColorPrimaries_ITU_R_709_2, .shouldPropagate)
        CVBufferSetAttachment(buf, kCVImageBufferTransferFunctionKey, kCVImageBufferTransferFunction_ITU_R_709_2, .shouldPropagate)
        CVBufferSetAttachment(buf, kCVImageBufferYCbCrMatrixKey, kCVImageBufferYCbCrMatrix_ITU_R_709_2, .shouldPropagate)
        CVPixelBufferLockBaseAddress(buf, [])
        defer { CVPixelBufferUnlockBaseAddress(buf, []) }
        guard let ctx = CGContext(data: CVPixelBufferGetBaseAddress(buf), width: W, height: H, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(buf), space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue) else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: W, height: H))
        return buf
    }

    /// Append `idx` as a new frame (img), repeating the previous frame for any skipped indices. Runs on q.
    func appendFrame(idx: Int, image: CGImage?, done: @escaping () -> Void) {
        q.async {
            defer { done() }
            let t0 = CACurrentMediaTime()
            var newBuf: CVPixelBuffer? = nil
            if let image = image { newBuf = self.makeBuffer(from: image) }
            self.procMs += (CACurrentMediaTime() - t0) * 1000; self.procN += 1
            self.pushUpTo(idx: idx, newBuf: newBuf)
        }
    }

    /// q only
    func pushUpTo(idx: Int, newBuf: CVPixelBuffer?) {
        var i = lastIdx + 1
        if i > idx { return }
        while i <= idx {
            let useNew = (i == idx) && newBuf != nil
            guard let b = useNew ? newBuf : (lastBuf ?? newBuf) else { i += 1; continue }
            var spins = 0
            while !input.isReadyForMoreMediaData && spins < 400 { Thread.sleep(forTimeInterval: 0.005); spins += 1 }
            if input.isReadyForMoreMediaData {
                if adaptor.append(b, withPresentationTime: CMTime(value: CMTimeValue(i), timescale: CMTimeScale(fps))) {
                    if useNew { written += 1 } else { repeated += 1 }
                } else { dropped += 1 }
            } else { dropped += 1 }
            i += 1
        }
        if let nb = newBuf { lastBuf = nb }
        lastIdx = idx
    }

    func finish(endIdx: Int, completion: @escaping () -> Void) {
        q.async {
            self.pushUpTo(idx: endIdx, newBuf: nil)
            self.input.markAsFinished()
            self.writer.endSession(atSourceTime: CMTime(value: CMTimeValue(max(self.lastIdx + 1, 1)), timescale: CMTimeScale(self.fps)))
            self.writer.finishWriting {
                if self.writer.status != .completed { err("writer error: \(String(describing: self.writer.error))") }
                completion()
            }
        }
    }
}

final class App: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler, WKUIDelegate {
    var win: NSWindow!
    var web: WKWebView!
    let o = opts
    let isInspect: Bool
    var loaded = false
    var started = false
    var stopping = false
    var sink: VideoSink?
    var t0 = 0.0
    var timer: DispatchSourceTimer?
    var slots: [NSBitmapImageRep?] = [nil, nil, nil, nil]
    var slotNext = 0
    let slotSem = DispatchSemaphore(value: 3)
    var markers: [[String: Any]] = []
    var markersPath = ""
    var cacheMs = 0.0, cgMs = 0.0, grabMs = 0.0, grabN = 0, skippedTicks = 0, lateTicks = 0
    var directorDone = false
    var zoom = 1.0
    var statT = 0.0, statGrabMs = 0.0, statN = 0, statRepeat0 = 0
    var endScheduled = false
    var consoleErrors = 0
    var sigSrc: [DispatchSourceSignal] = []

    override init() { isInspect = opts.shot != nil || opts.eval != nil || opts.dumpText; super.init() }

    func applicationDidFinishLaunching(_ n: Notification) {
        let backing = NSScreen.main?.backingScaleFactor ?? 2.0
        let dprWanted = o.dpr > 0 ? o.dpr : Double(o.outW) / Double(o.cssW)
        zoom = dprWanted / Double(backing)
        let vw = Double(o.cssW) * zoom, vh = Double(o.cssH) * zoom
        err(String(format: "[rec] css %dx%d, dpr %.3f (backing %.0f x pageZoom %.4f) -> view %.1fx%.1f pt, grab %.0fx%.0f px", o.cssW, o.cssH, dprWanted, backing, zoom, vw, vh, vw * backing, vh * backing))
        let rect = NSRect(x: -5000, y: -5000, width: vw, height: vh)
        win = NSWindow(contentRect: rect, styleMask: [.borderless], backing: .buffered, defer: false)
        win.isReleasedWhenClosed = false
        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(false, forKey: "hiddenPageDOMTimerThrottlingEnabled")
        cfg.preferences.setValue(false, forKey: "pageVisibilityBasedProcessSuppressionEnabled")
        cfg.mediaTypesRequiringUserActionForPlayback = .all
        if o.noWebSecurity { cfg.preferences.setValue(false, forKey: "webSecurityEnabled") }
        for k in o.insecureKeys { cfg.preferences.setValue(false, forKey: k) }
        if let prof = o.profile {
            let d = Array(SHA256.hash(data: Data(("rec-profile:" + prof).utf8)))
            var b = Array(d.prefix(16)); b[6] = (b[6] & 0x0f) | 0x50; b[8] = (b[8] & 0x3f) | 0x80
            let u = UUID(uuid: (b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]))
            cfg.websiteDataStore = WKWebsiteDataStore(forIdentifier: u)
            err("[rec] persistent profile '\(prof)' = \(u.uuidString)")
        } else {
            cfg.websiteDataStore = WKWebsiteDataStore.nonPersistent()
        }
        let ucc = cfg.userContentController
        ucc.add(self, name: "rec")
        if o.consoleAll { ucc.addUserScript(WKUserScript(source: "window.__recConsoleAll = true;", injectionTime: .atDocumentStart, forMainFrameOnly: true)) }
        ucc.addUserScript(WKUserScript(source: bridgeJS, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let libPath = o.lib ?? (exeDir + "/director_lib.js")
        if FileManager.default.fileExists(atPath: libPath) {
            ucc.addUserScript(WKUserScript(source: readFile(libPath), injectionTime: .atDocumentStart, forMainFrameOnly: true))
        } else if o.director != nil { err("[rec] WARNING: helper lib not found at \(libPath)") }
        for p in o.preJS { ucc.addUserScript(WKUserScript(source: readFile(p), injectionTime: .atDocumentStart, forMainFrameOnly: true)) }

        web = WKWebView(frame: NSRect(x: 0, y: 0, width: vw, height: vh), configuration: cfg)
        web.pageZoom = CGFloat(zoom)
        web.setValue(false, forKey: "windowOcclusionDetectionEnabled")
        web.navigationDelegate = self
        web.uiDelegate = self
        if #available(macOS 13.3, *) { web.isInspectable = true }
        win.contentView = web
        win.orderFrontRegardless()
        if o.out != "" { markersPath = o.out + ".markers.json" }

        // signals: finalise cleanly
        for s in [SIGINT, SIGTERM, SIGHUP] {
            signal(s, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: s, queue: .main)
            src.setEventHandler { [weak self] in err("[rec] signal \(s): stopping"); self?.stop(reason: "signal") }
            src.resume(); sigSrc.append(src)
        }
        guard let url = URL(string: o.url) else { err("bad url"); exit(2) }
        web.load(URLRequest(url: url))
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [weak self] in
            if let s = self, !s.loaded { err("[rec] page did not finish loading within 60 s; continuing anyway"); s.didLoad() }
        }
    }

    // MARK: navigation
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { didLoad() }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { err("[rec] navigation failed: \(error.localizedDescription)"); if !loaded { exit(3) } }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { err("[rec] load failed: \(error.localizedDescription)"); if !loaded { exit(3) } }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { err("[rec] WEB CONTENT PROCESS TERMINATED"); stop(reason: "webcontent-crash") }
    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let r = navigationResponse.response as? HTTPURLResponse, r.statusCode >= 400, navigationResponse.isForMainFrame { err("[rec] HTTP \(r.statusCode) for \(r.url?.absoluteString ?? "?")") }
        decisionHandler(.allow)
    }
    // self-signed TLS for 127.0.0.1 / localhost (used with tlsproxy.py so https pages can talk to the local http server)
    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let ps = challenge.protectionSpace
        if ps.authenticationMethod == NSURLAuthenticationMethodServerTrust, let trust = ps.serverTrust, (ps.host == "127.0.0.1" || ps.host == "localhost") {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else { completionHandler(.performDefaultHandling, nil) }
    }
    // JS dialogs: accept silently so nothing blocks
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) { err("[page alert] \(message)"); completionHandler() }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) { err("[page confirm->true] \(message)"); completionHandler(true) }
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) { err("[page prompt] \(prompt)"); completionHandler(defaultText) }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        err("[rec] popup requested: \(navigationAction.request.url?.absoluteString ?? "?")")
        if o.followPopups, let u = navigationAction.request.url { webView.load(URLRequest(url: u)) }
        return nil
    }

    func didLoad() {
        if loaded { return }
        loaded = true
        err("[rec] loaded \(web.url?.absoluteString ?? "?")")
        // Visibility check / activation policy
        checkVisibility { [weak self] in
            guard let self = self else { return }
            DispatchQueue.main.asyncAfter(deadline: .now() + self.o.settle) { self.afterLoad() }
        }
    }

    func checkVisibility(_ then: @escaping () -> Void) {
        let want = o.policy
        NSApp.setActivationPolicy(want == "regular" ? .regular : .accessory)
        win.orderFrontRegardless()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
            self.web.evaluateJavaScript("[document.hidden, document.visibilityState, document.hasFocus()]") { r, _ in
                let a = r as? [Any] ?? []
                let hidden = (a.first as? Bool) ?? ((a.first as? NSNumber)?.boolValue ?? false)
                err("[rec] page visibility: hidden=\(hidden) state=\(a.count > 1 ? "\(a[1])" : "?") focus=\(a.count > 2 ? "\(a[2])" : "?") policy=\(want == "regular" ? "regular" : "accessory")")
                if hidden && want == "auto" {
                    err("[rec] page hidden under .accessory -> switching to .regular")
                    NSApp.setActivationPolicy(.regular)
                    self.win.orderFrontRegardless()
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { then() }
                } else { then() }
            }
        }
    }

    func afterLoad() {
        if let ev = o.eval { runEval(ev); return }
        if o.dumpText { runEval("document.body ? document.body.innerText : ''"); return }
        if let shot = o.shot {
            if o.director != nil { startDirector() }
            DispatchQueue.main.asyncAfter(deadline: .now() + o.after) { self.takeShot(shot) }
            return
        }
        startRecording()
    }

    // MARK: inspect modes
    func runEval(_ code: String) {
        DispatchQueue.main.asyncAfter(deadline: .now() + (o.eval != nil || o.dumpText ? o.after : 0)) {
            let body = """
            const AF = Object.getPrototypeOf(async function(){}).constructor;
            let f; try { f = new AF('return (' + code + '\\n)'); } catch (e) { f = new AF(code); }
            return await f();
            """
            self.web.callAsyncJavaScript(body, arguments: ["code": code], in: nil, in: .page) { res in
                switch res {
                case .success(let v):
                    if let s = v as? String { print(s) }
                    else if v is NSNull { print("null") }
                    else if JSONSerialization.isValidJSONObject(v), let d = try? JSONSerialization.data(withJSONObject: v, options: [.prettyPrinted, .sortedKeys]) { print(String(data: d, encoding: .utf8) ?? "") }
                    else { print("\(v)") }
                    exit(0)
                case .failure(let e): err("[rec] eval error: \(e.localizedDescription)"); exit(1)
                }
            }
        }
    }

    func grabRep() -> NSBitmapImageRep? {
        let b = web.bounds
        let i = slotNext; slotNext = (slotNext + 1) % slots.count
        if slots[i] == nil { slots[i] = web.bitmapImageRepForCachingDisplay(in: b) }
        guard let rep = slots[i] else { return nil }
        web.cacheDisplay(in: b, to: rep)
        return rep
    }

    func takeShot(_ path: String) {
        guard let rep = web.bitmapImageRepForCachingDisplay(in: web.bounds) else { err("[rec] no bitmap"); exit(1) }
        web.cacheDisplay(in: web.bounds, to: rep)
        guard let cg = rep.cgImage else { err("[rec] no cgimage"); exit(1) }
        let cs = CGColorSpace(name: CGColorSpace.sRGB)!
        let ctx = CGContext(data: nil, width: o.outW, height: o.outH, bitsPerComponent: 8, bytesPerRow: 0, space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)!
        ctx.interpolationQuality = .high
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: o.outW, height: o.outH))
        let img = ctx.makeImage()!
        let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL, UTType.png.identifier as CFString, 1, nil)!
        CGImageDestinationAddImage(dest, img, nil)
        CGImageDestinationFinalize(dest)
        err("[rec] shot \(path) (\(rep.pixelsWide)x\(rep.pixelsHigh) -> \(o.outW)x\(o.outH)) errors=\(consoleErrors)")
        exit(0)
    }

    // MARK: recording
    func startRecording() {
        do { sink = try VideoSink(url: URL(fileURLWithPath: o.out), W: o.outW, H: o.outH, fps: o.fps, bitrate: o.bitrate, fragment: o.fragment) }
        catch { err("[rec] cannot create writer: \(error)"); exit(1) }
        try? "[]".write(toFile: markersPath, atomically: true, encoding: .utf8)
        started = true
        t0 = CACurrentMediaTime()
        web.evaluateJavaScript("window.rec && (rec._t0 = Date.now())", completionHandler: nil)
        err("[rec] recording -> \(o.out)  \(o.outW)x\(o.outH)@\(o.fps)  css \(o.cssW)x\(o.cssH)  max \(o.maxSeconds)s")
        tick()   // frame 0 immediately
        let tm = DispatchSource.makeTimerSource(flags: .strict, queue: .main)
        tm.schedule(deadline: .now() + 1.0 / Double(o.fps), repeating: 1.0 / Double(o.fps), leeway: .microseconds(200))
        tm.setEventHandler { [weak self] in self?.tick() }
        tm.resume(); timer = tm
        DispatchQueue.main.asyncAfter(deadline: .now() + o.maxSeconds) { [weak self] in self?.stop(reason: "max-seconds") }
        if o.director != nil { startDirector() }
    }

    func tick() {
        guard started, !stopping, let sink = sink else { return }
        let now = CACurrentMediaTime()
        let idx = Int(floor((now - t0) * Double(o.fps)))
        if idx <= sink.lastIdxMain { skippedTicks += 1; return }
        if idx > sink.lastIdxMain + 1 && sink.lastIdxMain >= 0 { lateTicks += 1 }
        if slotSem.wait(timeout: .now()) != .success {   // encoder queue is behind: repeat last frame instead of blocking
            sink.lastIdxMain = idx
            sink.appendFrame(idx: idx, image: nil) {}
            return
        }
        let g0 = CACurrentMediaTime()
        guard let rep = grabRep() else { slotSem.signal(); return }
        let g1 = CACurrentMediaTime()
        guard let cg = rep.cgImage else { slotSem.signal(); return }
        cacheMs += (g1 - g0) * 1000; cgMs += (CACurrentMediaTime() - g1) * 1000
        grabMs += (CACurrentMediaTime() - g0) * 1000; grabN += 1
        statGrabMs += (CACurrentMediaTime() - g0) * 1000; statN += 1
        if now - t0 - statT >= 15 {
            err(String(format: "[rec %6.1f] last %.0fs: %d grabs, avg %.0f ms each (%.1f grabs/s), %d slots-busy repeats", now - t0, now - t0 - statT, statN, statGrabMs / Double(max(statN, 1)), Double(statN) / (now - t0 - statT), 0))
            statT = now - t0; statGrabMs = 0; statN = 0
        }
        sink.lastIdxMain = idx
        sink.appendFrame(idx: idx, image: cg) { [slotSem] in slotSem.signal() }
    }

    func stop(reason: String) {
        if stopping { return }
        stopping = true
        timer?.cancel()
        if !started { exit(0) }
        let dur = CACurrentMediaTime() - t0
        let endIdx = Int(ceil(dur * Double(o.fps))) - 1
        err("[rec] stopping (\(reason)) after \(String(format: "%.2f", dur)) s")
        guard let sink = sink else { exit(1) }
        sink.finish(endIdx: max(endIdx, sink.lastIdxMain)) {
            let s = sink
            let meta: [String: Any] = [
                "file": self.o.out, "duration": Double(s.lastIdx + 1) / Double(self.o.fps), "wall_seconds": dur, "fps": self.o.fps,
                "width": self.o.outW, "height": self.o.outH, "css": "\(self.o.cssW)x\(self.o.cssH)", "frames": s.lastIdx + 1,
                "grabbed": s.written, "repeated": s.repeated, "dropped": s.dropped, "stop_reason": reason,
                "avg_grab_ms": self.grabN > 0 ? self.grabMs / Double(self.grabN) : 0, "avg_cacheDisplay_ms": self.grabN > 0 ? self.cacheMs / Double(self.grabN) : 0, "avg_cgimage_ms": self.grabN > 0 ? self.cgMs / Double(self.grabN) : 0, "avg_encode_prep_ms": s.procN > 0 ? s.procMs / Double(s.procN) : 0,
                "console_errors": self.consoleErrors,
            ]
            if let d = try? JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted, .sortedKeys]) {
                try? d.write(to: URL(fileURLWithPath: self.o.out + ".meta.json"))
                err("[rec] done: " + (String(data: d, encoding: .utf8) ?? "").replacingOccurrences(of: "\n", with: " "))
            }
            exit(0)
        }
    }

    // MARK: director
    func startDirector() {
        guard let path = o.director else { return }
        let body = readFile(path)
        let js = "(async()=>{try{await (async function(){" + body + "\n})();rec.log('director finished');}catch(e){rec.log('DIRECTOR ERROR: '+(e&&e.stack||e));}rec.done();})();"
        web.evaluateJavaScript(js) { _, e in
            if let e = e as NSError?, e.code == WKError.javaScriptExceptionOccurred.rawValue {
                err("[rec] director script error: \(e.localizedDescription) \(e.userInfo["WKJavaScriptExceptionMessage"] ?? "") line \(e.userInfo["WKJavaScriptExceptionLineNumber"] ?? "?")")
                self.directorEnded()
            }
        }
    }

    func directorEnded() {
        if directorDone { return }
        directorDone = true
        if isInspect { return }
        err("[rec] director ended at t=\(String(format: "%.2f", CACurrentMediaTime() - t0)); tail \(o.tail)s")
        DispatchQueue.main.asyncAfter(deadline: .now() + o.tail) { [weak self] in self?.stop(reason: "director-done") }
    }

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let m = message.body as? [String: Any], let type = m["type"] as? String else { return }
        switch type {
        case "mark":
            let t = started ? CACurrentMediaTime() - t0 : 0
            var e: [String: Any] = ["name": m["name"] ?? "", "t": (t * 1000).rounded() / 1000]
            e["data"] = m["data"] is NSNull ? NSNull() : (m["data"] ?? NSNull())
            markers.append(e)
            err(String(format: "[mark %7.2f] %@", t, "\(m["name"] ?? "")"))
            if !markersPath.isEmpty, let d = try? JSONSerialization.data(withJSONObject: markers, options: [.prettyPrinted]) { try? d.write(to: URL(fileURLWithPath: markersPath)) }
        case "log":
            let t = started ? CACurrentMediaTime() - t0 : 0
            err(String(format: "[log  %7.2f] %@", t, "\(m["msg"] ?? "")"))
        case "console":
            let lvl = "\(m["level"] ?? "")"
            if lvl == "error" { consoleErrors += 1 }
            err("[console.\(lvl)] \(m["msg"] ?? "")")
        case "done": directorEnded()
        default: break
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = App()
app.delegate = delegate
app.run()
