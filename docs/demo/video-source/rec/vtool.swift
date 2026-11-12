// vtool: tiny mp4 inspector (no ffmpeg here).  swiftc -O -swift-version 5 vtool.swift -o vtool
//   vtool info f.mp4                        duration, size, fps, bitrate, frame count, pts regularity
//   vtool frame f.mp4 T out.png [width]     extract frame at T seconds (optionally scaled to width)
//   vtool dups f.mp4 [start end]            count consecutive-identical frames (sampled hash) = repeated grabs
//   vtool stats f.mp4 T1 T2 ...             mean luma (0-255) + "busy" (fraction of non-background-ish variance) at times
//   vtool timeline f.mp4 [step]             one line per `step` seconds (default 5): mean luma + change vs previous sample
import AVFoundation
import CoreImage
import AppKit
import ImageIO
import UniformTypeIdentifiers

setvbuf(stdout, nil, _IONBF, 0)
let a = CommandLine.arguments
if a.count < 3 { print("usage: vtool info|frame|dups|stats|timeline file ..."); exit(2) }
let cmd = a[1]
let url = URL(fileURLWithPath: a[2])
let asset = AVURLAsset(url: url)
let sem = DispatchSemaphore(value: 0)

func track() -> AVAssetTrack {
    var t: AVAssetTrack?
    Task { t = try? await asset.loadTracks(withMediaType: .video).first; sem.signal() }
    sem.wait()
    guard let tr = t else { print("no video track"); exit(1) }
    return tr
}
func dur() -> Double {
    var d = 0.0
    Task { d = ((try? await asset.load(.duration)) ?? .zero).seconds; sem.signal() }
    sem.wait(); return d
}

func lumaOf(_ img: CGImage) -> (Double, Double) {
    let w = 64, h = 36
    let cs = CGColorSpaceCreateDeviceGray()
    var px = [UInt8](repeating: 0, count: w * h)
    px.withUnsafeMutableBytes { p in
        let c = CGContext(data: p.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w, space: cs, bitmapInfo: CGImageAlphaInfo.none.rawValue)!
        c.interpolationQuality = .medium
        c.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
    }
    let mean = Double(px.reduce(0) { $0 + Int($1) }) / Double(w * h)
    let v = px.reduce(0.0) { $0 + pow(Double($1) - mean, 2) } / Double(w * h)
    return (mean, v.squareRoot())
}
func thumb(_ img: CGImage, _ w: Int, _ h: Int) -> [UInt8] {
    var px = [UInt8](repeating: 0, count: w * h)
    px.withUnsafeMutableBytes { p in
        let c = CGContext(data: p.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w, space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)!
        c.interpolationQuality = .medium
        c.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
    }
    return px
}
func gen(at t: Double) -> CGImage? {
    let g = AVAssetImageGenerator(asset: asset)
    g.requestedTimeToleranceBefore = .zero; g.requestedTimeToleranceAfter = .zero
    g.appliesPreferredTrackTransform = true
    return try? g.copyCGImage(at: CMTime(seconds: t, preferredTimescale: 600), actualTime: nil)
}

switch cmd {
case "info":
    let tr = track()
    var size = CGSize.zero, fps: Float = 0, br: Float = 0
    Task { size = (try? await tr.load(.naturalSize)) ?? .zero; fps = (try? await tr.load(.nominalFrameRate)) ?? 0; br = (try? await tr.load(.estimatedDataRate)) ?? 0; sem.signal() }
    sem.wait()
    let reader = try! AVAssetReader(asset: asset)
    let out = AVAssetReaderTrackOutput(track: tr, outputSettings: nil)
    reader.add(out); reader.startReading()
    var n = 0, last = -1.0, minDt = 1e9, maxDt = 0.0, first = -1.0, lastPts = 0.0
    while let s = out.copyNextSampleBuffer() {
        if CMSampleBufferGetNumSamples(s) == 0 { continue }
        let t = CMSampleBufferGetPresentationTimeStamp(s).seconds
        if first < 0 { first = t }
        if last >= 0 { minDt = min(minDt, t - last); maxDt = max(maxDt, t - last) }
        last = t; lastPts = t; n += 1
    }
    print(String(format: "duration %.3f s  size %.0fx%.0f  nominal %.2f fps  rate %.2f Mbps  frames %d  first_pts %.3f last_pts %.3f  dt min %.4f max %.4f", dur(), size.width, size.height, fps, Double(br) / 1e6, n, first, lastPts, minDt, maxDt))
case "frame":
    let t = Double(a[3])!
    guard let img = gen(at: t) else { print("no frame at \(t)"); exit(1) }
    var out = img
    if a.count > 5, let w = Int(a[5]) {
        let h = Int(Double(w) * Double(img.height) / Double(img.width))
        let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue)!
        ctx.interpolationQuality = .high
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
        out = ctx.makeImage()!
    }
    let d = CGImageDestinationCreateWithURL(URL(fileURLWithPath: a[4]) as CFURL, UTType.png.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(d, out, nil); CGImageDestinationFinalize(d)
    print("wrote \(a[4]) \(out.width)x\(out.height)")
case "dups":
    let tr = track()
    let s0 = a.count > 3 ? Double(a[3])! : 0, s1 = a.count > 4 ? Double(a[4])! : dur()
    let reader = try! AVAssetReader(asset: asset)
    reader.timeRange = CMTimeRange(start: CMTime(seconds: s0, preferredTimescale: 600), duration: CMTime(seconds: s1 - s0, preferredTimescale: 600))
    let out = AVAssetReaderTrackOutput(track: tr, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
    reader.add(out); reader.startReading()
    var n = 0, dups = 0
    var prev: [UInt8] = []
    var runs: [Int] = [], run = 0, maxRun = 0
    while let s = out.copyNextSampleBuffer() {
        guard let pb = CMSampleBufferGetImageBuffer(s) else { continue }
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb), bpr = CVPixelBufferGetBytesPerRow(pb)
        let base = CVPixelBufferGetBaseAddress(pb)!.assumingMemoryBound(to: UInt8.self)
        var cur = [UInt8](); cur.reserveCapacity(96 * 54)
        for y in stride(from: 3, to: h, by: 20) { for x in stride(from: 3, to: w, by: 20) { let o = y * bpr + x * 4; cur.append(base[o]); cur.append(base[o + 1]); cur.append(base[o + 2]) } }
        CVPixelBufferUnlockBaseAddress(pb, .readOnly)
        var md = 0.0
        if n > 0 && cur.count == prev.count { var sum = 0; for i in 0..<cur.count { sum += abs(Int(cur[i]) - Int(prev[i])) }; md = Double(sum) / Double(cur.count) }
        if n > 0 && md < 0.35 { dups += 1; run += 1; maxRun = max(maxRun, run) } else { if run > 0 { runs.append(run) }; run = 0 }
        prev = cur; n += 1
    }
    print(String(format: "frames %d  identical-to-previous %d (%.1f%%)  longest static run %d frames (%.2fs)", n, dups, 100.0 * Double(dups) / Double(max(n, 1)), maxRun, Double(maxRun) / 30.0))
case "motion":
    // vtool motion f.mp4 start end   : per-second count of frames that differ (dense sampling, >12 levels in >=6 samples) from the previous frame
    let tr = track()
    let s0 = Double(a[3])!, s1 = Double(a[4])!
    let lvl = a.count > 5 ? Int(a[5])! : 3, cnt = a.count > 6 ? Int(a[6])! : 1
    let reader = try! AVAssetReader(asset: asset)
    reader.timeRange = CMTimeRange(start: CMTime(seconds: s0, preferredTimescale: 600), duration: CMTime(seconds: s1 - s0, preferredTimescale: 600))
    let out = AVAssetReaderTrackOutput(track: tr, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
    reader.add(out); reader.startReading()
    var prev: [UInt8] = []; var uniq = 0, total = 0, sec = Int(s0); var line = ""; var allUniq = 0, allTotal = 0
    while let s = out.copyNextSampleBuffer() {
        guard let pb = CMSampleBufferGetImageBuffer(s) else { continue }
        let pts = CMSampleBufferGetPresentationTimeStamp(s).seconds
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb), bpr = CVPixelBufferGetBytesPerRow(pb)
        let base = CVPixelBufferGetBaseAddress(pb)!.assumingMemoryBound(to: UInt8.self)
        var cur = [UInt8](); cur.reserveCapacity(480 * 270)
        for y in stride(from: 2, to: h, by: 4) { for x in stride(from: 2, to: w, by: 4) { cur.append(base[y * bpr + x * 4 + 1]) } }
        CVPixelBufferUnlockBaseAddress(pb, .readOnly)
        var changed = 0
        if !prev.isEmpty { for i in 0..<cur.count where abs(Int(cur[i]) - Int(prev[i])) > lvl { changed += 1 } }
        let isNew = prev.isEmpty || changed >= cnt
        if Int(pts) != sec { line += String(format: " %d:%d/%d", sec, uniq, total); sec = Int(pts); allUniq += uniq; allTotal += total; uniq = 0; total = 0 }
        total += 1; if isNew { uniq += 1 }
        prev = cur
    }
    print("per-second unique/total frames:" + line)
    print(String(format: "overall unique %d / %d  = %.1f fps", allUniq, allTotal, Double(allUniq) / max(Double(allTotal), 1) * 30))
case "stats":
    for ts in a.dropFirst(3) {
        let t = Double(ts)!
        if let img = gen(at: t) { let (m, sd) = lumaOf(img); print(String(format: "t=%.2f mean_luma=%.1f stddev=%.1f", t, m, sd)) } else { print("t=\(t) no frame") }
    }
case "timeline":
    let step = a.count > 3 ? Double(a[3])! : 5
    let total = dur()
    var prev: [UInt8] = []
    var t = 0.0
    while t < total - 0.05 {
        if let img = gen(at: t) {
            let (m, sd) = lumaOf(img)
            let cur = thumb(img, 48, 27)
            var diff = 0.0
            if !prev.isEmpty { var s = 0; for i in 0..<cur.count { s += abs(Int(cur[i]) - Int(prev[i])) }; diff = Double(s) / Double(cur.count) }
            prev = cur
            print(String(format: "t=%7.2f luma=%5.1f sd=%5.1f change_vs_prev=%5.1f", t, m, sd, diff))
        }
        t += step
    }
default: print("unknown cmd")
}
