import AVFoundation
import Combine
import Foundation
import ImageIO
import UIKit

struct AdviceCard: Identifiable, Equatable {
    let id = UUID()
    let title: String
    let bullets: [String]
}

final class PhoneCameraController: NSObject, AVCapturePhotoCaptureDelegate {
    let session = AVCaptureSession()
    private let sessionQueue = DispatchQueue(label: "com.kanjian.phone-camera")
    private let photoOutput = AVCapturePhotoOutput()
    private var isConfigured = false
    private var pendingPhotoCompletion: ((UIImage?) -> Void)?

    func start() {
        sessionQueue.async { [weak self] in
            guard let self else { return }
            if !self.isConfigured { self.configure() }
            guard self.isConfigured, !self.session.isRunning else { return }
            self.session.startRunning()
        }
    }

    func stop() {
        sessionQueue.async { [weak self] in
            guard let self, self.session.isRunning else { return }
            self.session.stopRunning()
        }
    }

    private func configure() {
        guard AVCaptureDevice.authorizationStatus(for: .video) == .authorized,
              let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .front),
              let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input) else { return }

        session.beginConfiguration()
        // The preview panel is only 280x400 and the analysis service does not
        // need a full sensor-resolution frame. Keeping this at 480p avoids
        // allocating several multi-megapixel UIImages per second.
        session.sessionPreset = .medium
        session.addInput(input)
        guard session.canAddOutput(photoOutput) else {
            session.commitConfiguration()
            return
        }
        session.addOutput(photoOutput)
        configureVideoConnection(photoOutput.connection(with: .video))
        session.commitConfiguration()
        isConfigured = true
    }

    /// The app is portrait-only. Camera sensors commonly deliver a landscape
    /// pixel buffer, so the connection must carry the portrait rotation before
    /// the frame is shown or sent to the analysis service.
    private func configureVideoConnection(_ connection: AVCaptureConnection?) {
        guard let connection else { return }
        if connection.isVideoRotationAngleSupported(90) {
            connection.videoRotationAngle = 90
        } else if connection.isVideoOrientationSupported {
            connection.videoOrientation = .portrait
        }
        if connection.isVideoMirroringSupported {
            connection.automaticallyAdjustsVideoMirroring = false
            connection.isVideoMirrored = true
        }
    }

    func capturePhoto(completion: @escaping (UIImage?) -> Void) {
        sessionQueue.async { [weak self] in
            guard let self, self.isConfigured, self.session.isRunning else {
                DispatchQueue.main.async { completion(nil) }
                return
            }
            self.pendingPhotoCompletion = completion
            let settings = AVCapturePhotoSettings()
            settings.flashMode = .off
            self.photoOutput.capturePhoto(with: settings, delegate: self)
        }
    }

    func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        let completion = pendingPhotoCompletion
        pendingPhotoCompletion = nil
        let image = error == nil ? photo.fileDataRepresentation().flatMap(UIImage.init(data:)) : nil
        DispatchQueue.main.async { completion?(image) }
    }
}

final class BoardMJPEGController: NSObject, URLSessionDataDelegate {
    private let jpegStart = Data([0xFF, 0xD8])
    private let jpegEnd = Data([0xFF, 0xD9])
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var buffer = Data()
    private var lastDeliveredAt = Date.distantPast
    // The display is small; decoding the board's full camera JPG wastes CPU and
    // makes SwiftUI repaint large images faster than the UI can consume them.
    private let maxPreviewPixels = 720
    private let minimumFrameInterval: TimeInterval = 1.0 / 15.0
    var onFrame: ((UIImage) -> Void)?
    var onError: ((Error?) -> Void)?

    func start(url: URL) {
        stop()
        buffer.removeAll(keepingCapacity: true)
        lastDeliveredAt = .distantPast
        let configuration = URLSessionConfiguration.default
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 15
        let delegateQueue = OperationQueue()
        delegateQueue.maxConcurrentOperationCount = 1
        session = URLSession(configuration: configuration, delegate: self, delegateQueue: delegateQueue)
        task = session?.dataTask(with: URLRequest(url: url))
        task?.resume()
    }

    func stop() {
        task?.cancel()
        task = nil
        session?.invalidateAndCancel()
        session = nil
        buffer.removeAll(keepingCapacity: true)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer.append(data)
        while let start = buffer.range(of: jpegStart) {
            if start.lowerBound > 0 { buffer.removeSubrange(0..<start.lowerBound) }
            guard let end = buffer.range(of: jpegEnd, in: start.upperBound..<buffer.endIndex) else { return }
            let imageData = buffer.subdata(in: start.lowerBound..<end.upperBound)
            buffer.removeSubrange(0..<end.upperBound)
            let now = Date()
            guard now.timeIntervalSince(lastDeliveredAt) >= minimumFrameInterval else { continue }
            lastDeliveredAt = now
            if let image = Self.downsampledImage(from: imageData, maxPixelSize: maxPreviewPixels) {
                onFrame?(image)
            }
        }
    }

    private static func downsampledImage(from data: Data, maxPixelSize: Int) -> UIImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixelSize
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        return UIImage(cgImage: image)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if error != nil { onError?(error) }
    }
}

enum CameraSource: String, CaseIterable, Identifiable {
    case testData, board, phone
    var id: String { rawValue }
    var title: String {
        switch self { case .testData: return "化妆测试图"; case .board: return "外貌check"; case .phone: return "手机" }
    }
    var icon: String {
        switch self { case .testData: return "photo"; case .board: return "cpu"; case .phone: return "camera" }
    }

    static let userSelectable: [CameraSource] = [.phone, .board]
}

enum MirrorStage: Equatable {
    case live, frozen, analyzing, advice, effect
}

@MainActor
final class KanjianViewModel: ObservableObject {
    // Roadshow-only fixture. The production proxy methods below are unchanged;
    // flip this to false to resume the real K3 / image-generation flow.
    private let roadshowDemoMode = true
    private let roadshowAdviceCards = [
        AdviceCard(title: "眼妆", bullets: [
            "用哑光米杏色铺满上眼睑，眼尾叠一层深棕色，让镜头里的眼部轮廓更清晰。",
            "棕色内眼线只沿睫毛根部填补，眼尾延长 1–2 mm；睫毛夹翘后刷一层纤长睫毛膏。"
        ]),
        AdviceCard(title: "腮红与面中", bullets: [
            "用奶油杏粉色腮红从眼下约 1 cm 横向扫向太阳穴，并轻带过鼻梁中段。",
            "定妆时只轻压 T 区，保留颧骨高点的自然光泽，避免镜头里显得过平。"
        ]),
        AdviceCard(title: "唇妆", bullets: [
            "选低饱和蜜桃奶茶色，薄涂全唇后晕开唇缘，做柔雾感的 MLBB 唇。",
            "唇峰点少量透明唇蜜即可，保留一点光泽但不要大面积反光。"
        ])
    ]
    // The camera is served directly by the RDK X5. Keep model requests on the
    // Mac proxy so API keys never need to be copied to the board or the app.
    @Published var cameraServerURL = "http://192.168.43.93:8080"
    // Current Mac proxy address on the same Wi-Fi as the RDK X5/iPhone.
    // Change this when the Mac changes networks; the board URL is independent.
    @Published var hardwareImage: UIImage?
    @Published var result = "收到开发板镜头画面后，点击“外貌check”。"
    @Published var roadshowLabel: String?
    @Published var connectionText = "未连接"
    @Published var stage: MirrorStage = .live
    @Published var frozenImage: UIImage?
    @Published var generatedImage: UIImage?
    @Published var adviceCards: [AdviceCard] = []
    @Published var currentCardIndex = 0
    @Published var isGenerating = false
    @Published var cameraSource: CameraSource = .board
    let phoneCamera = PhoneCameraController()
    let boardStream = BoardMJPEGController()

    private var pollTask: Task<Void, Never>?

    init() {
        boardStream.onFrame = { [weak self] image in
            Task { @MainActor in
                guard let self, self.cameraSource == .board else { return }
                self.hardwareImage = image
                self.connectionText = "已连接"
            }
        }
        boardStream.onError = { [weak self] error in
            Task { @MainActor in
                guard let self, self.cameraSource == .board else { return }
                self.connectionText = "连接中"
                self.result = "开发板视频流中断：\(Self.describe(error ?? DemoError.serverError))"
            }
        }
    }

    func startPolling() {
        pollTask?.cancel()
        if roadshowDemoMode {
            cameraSource = .testData
            loadTestCameraFrame()
            connectionText = "演示画面"
            roadshowLabel = "路演演示 · 本地占位数据"
            return
        }
        if cameraSource == .testData { loadTestCameraFrame(); connectionText = "测试图片" }
        if cameraSource == .board { startBoardStream() }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
        phoneCamera.stop()
        boardStream.stop()
    }

    func selectCameraSource(_ source: CameraSource) {
        cameraSource = source
        switch source {
        case .testData:
            phoneCamera.stop()
            loadTestCameraFrame()
            connectionText = "测试图片"
        case .board:
            phoneCamera.stop()
            hardwareImage = nil
            connectionText = "连接中"
            startBoardStream()
        case .phone:
            connectionText = "手机摄像头"
            Task { await startPhoneCamera() }
        }
    }

    private func startBoardStream() {
        guard let url = URL(string: cameraServerURL + "/stream.mjpg") else {
            connectionText = "地址无效"
            return
        }
        boardStream.start(url: url)
    }

    private func startPhoneCamera() async {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            phoneCamera.start()
        case .notDetermined:
            if await AVCaptureDevice.requestAccess(for: .video) {
                phoneCamera.start()
            } else {
                connectionText = "未允许相机权限"
            }
        default:
            connectionText = "请在设置中允许相机"
        }
    }

    private func loadTestCameraFrame() {
        if roadshowDemoMode {
            hardwareImage = UIImage(named: "roadshow-before-v1") ?? UIImage(named: "makeup-test") ?? Self.makeCleanTestFrame()
        } else {
            hardwareImage = UIImage(named: "makeup-test") ?? Self.makeCleanTestFrame()
        }
    }

    private static func makeCleanTestFrame() -> UIImage {
        let size = CGSize(width: 1080, height: 1440)
        return UIGraphicsImageRenderer(size: size).image { context in
            let bounds = CGRect(origin: .zero, size: size)
            UIColor(red: 0.62, green: 0.80, blue: 0.95, alpha: 1).setFill()
            context.fill(bounds)
            UIColor(red: 0.22, green: 0.21, blue: 0.18, alpha: 0.42).setStroke()
            context.cgContext.setLineWidth(4)
            context.cgContext.stroke(bounds.insetBy(dx: 32, dy: 32))
            let text = "测试画面"
            let attributes: [NSAttributedString.Key: Any] = [
                .font: UIFont.systemFont(ofSize: 42, weight: .medium),
                .foregroundColor: UIColor(red: 0.22, green: 0.21, blue: 0.18, alpha: 0.62)
            ]
            let textSize = text.size(withAttributes: attributes)
            text.draw(at: CGPoint(x: (size.width - textSize.width) / 2, y: (size.height - textSize.height) / 2), withAttributes: attributes)
        }
    }

    func freezeCurrentFrame() {
        if cameraSource == .phone {
            connectionText = "拍摄中"
            phoneCamera.capturePhoto { [weak self] image in
                guard let self else { return }
                guard let image else {
                    self.connectionText = "手机摄像头"
                    self.result = "手机照片拍摄失败，请重试。"
                    return
                }
                self.freeze(image)
            }
            return
        }
        guard let image = hardwareImage else {
            result = "还没有收到镜头画面。"
            return
        }
        freeze(image)
    }

    private func freeze(_ image: UIImage) {
        frozenImage = image
        generatedImage = nil
        stage = .frozen
    }

    func returnToLive() {
        stage = .live
        generatedImage = nil
        currentCardIndex = 0
    }

    func startAnalysis() {
        guard frozenImage != nil, stage != .analyzing else { return }
        stage = .analyzing
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(900))
            guard !Task.isCancelled else { return }
            adviceCards = roadshowAdviceCards
            currentCardIndex = 0
            result = "本地建议已就绪 · 演示数据"
            roadshowLabel = "妆容建议 · 本地演示数据"
            stage = .advice
        }
    }

    func nextCard() { currentCardIndex = min(currentCardIndex + 1, adviceCards.count - 1) }
    func previousCard() { currentCardIndex = max(currentCardIndex - 1, 0) }

    var currentAdviceCard: AdviceCard? {
        guard adviceCards.indices.contains(currentCardIndex) else { return nil }
        return adviceCards[currentCardIndex]
    }

    func generateMakeupEffect() {
        guard frozenImage != nil else { return }
        isGenerating = true
        stage = .effect
        generatedImage = nil
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(1200))
            guard !Task.isCancelled else { return }
            generatedImage = UIImage(named: "roadshow-after-v1")
            result = generatedImage == nil ? "演示效果图资源缺失。" : "本地演示效果图已生成"
            roadshowLabel = "本地演示效果图"
            isGenerating = false
        }
    }

    func saveGeneratedImage() {
        guard let generatedImage else { return }
        UIImageWriteToSavedPhotosAlbum(generatedImage, nil, nil, nil)
        result = "效果图已保存到照片。"
    }

    private static func describe(_ error: Error) -> String {
        if let urlError = error as? URLError {
            return urlError.localizedDescription
        }
        return error.localizedDescription
    }

    private enum DemoError: Error { case serverError, imageEncodingFailed }
}
