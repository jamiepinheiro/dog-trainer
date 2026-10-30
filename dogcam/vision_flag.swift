// Dog flag via macOS Vision animal recognition. Usage: vision_flag <image.jpg>
// Prints JSON: [{"label":"dog","confidence":0.9,"bbox":[x,y,w,h]}] (normalized, top-left origin)
import Foundation
import Vision
import ImageIO

guard CommandLine.arguments.count == 2 else { fputs("usage: vision_flag <image>\n", stderr); exit(2) }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
      let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { print("[]"); exit(0) }

let req = VNRecognizeAnimalsRequest()
let handler = VNImageRequestHandler(cgImage: img, options: [:])
try handler.perform([req])
var out: [[String: Any]] = []
for obs in req.results ?? [] {
    for id in obs.labels where id.identifier.lowercased().contains("dog") || id.identifier.lowercased().contains("cat") {
        let b = obs.boundingBox // normalized, bottom-left origin
        out.append(["label": id.identifier, "confidence": Double(id.confidence),
                    "bbox": [Double(b.origin.x), Double(1 - b.origin.y - b.height), Double(b.width), Double(b.height)]])
    }
}
let data = try JSONSerialization.data(withJSONObject: out)
print(String(data: data, encoding: .utf8)!)
