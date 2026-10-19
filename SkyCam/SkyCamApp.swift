import AVFoundation
import SwiftUI

@main
struct SkyCamApp: App {
    @StateObject private var model = AppModel.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .onAppear {
                    UIApplication.shared.isIdleTimerDisabled = true
                    model.start()
                }
        }
    }
}

/// Red-on-black night screen: live view for aiming, then the address and token
/// the Mac needs, the current settings and the last few log lines.
struct ContentView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            VStack(alignment: .leading, spacing: 10) {
                CameraPreview(session: model.camera.session)
                    .aspectRatio(3.0 / 4.0, contentMode: .fit)
                    .overlay(Color.black.opacity(model.dimmed ? 0.92 : 0))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                Text("SkyCam \(model.version) · \(model.serverState)")
                    .font(.system(.footnote, design: .monospaced).bold())
                ForEach(model.addresses, id: \.self) { a in
                    Text("http://\(a.split(separator: " ").last ?? ""):\(model.port)")
                }
                Text("token \(model.token)")
                Text(model.statusLine)
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(model.recentLog, id: \.self) { Text($0).lineLimit(1) }
                }
                .font(.system(size: 10, design: .monospaced))
                .opacity(0.8)
                Spacer(minLength: 0)
                Button(model.dimmed ? "Brighten screen" : "Dim screen") { model.setDimmed(!model.dimmed) }
                    .buttonStyle(.bordered)
                    .tint(.red)
            }
            .font(.system(.footnote, design: .monospaced))
            .foregroundStyle(Color(red: 0.85, green: 0.1, blue: 0.1))
            .padding(12)
        }
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.previewLayer.session = session
        v.previewLayer.videoGravity = .resizeAspect
        v.backgroundColor = .black
        return v
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {}

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }
}
