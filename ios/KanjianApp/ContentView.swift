import AVFoundation
import Foundation
import SwiftUI

private enum MirrorPalette {
    static let canvas = Color(red: 0.89, green: 0.88, blue: 0.83)
    static let paper = Color(red: 0.98, green: 0.97, blue: 0.93)
    static let ink = Color(red: 0.22, green: 0.21, blue: 0.18)
    static let ochre = Color(red: 0.67, green: 0.45, blue: 0.08)
    static let muted = Color(red: 0.47, green: 0.46, blue: 0.41)
}

struct ContentView: View {
    @EnvironmentObject private var model: KanjianViewModel
    @State private var showSourceMenu = false
    @State private var cardFlipAngle = 0.0
    @State private var isCardFlipping = false

    var body: some View {
        GeometryReader { proxy in
            ZStack {
                MirrorPalette.canvas.ignoresSafeArea()
                switch model.stage {
                case .live: liveView
                case .frozen, .analyzing: frozenView
                case .advice: adviceView
                case .effect: effectView
                }
            }
            .frame(width: proxy.size.width, height: proxy.size.height)
        }
        .task { model.startPolling() }
        .onDisappear { model.stopPolling() }
        .ignoresSafeArea(edges: model.stage == .live ? .all : [])
        .statusBarHidden(model.stage == .live || model.stage == .frozen || model.stage == .analyzing)
    }

    private var liveView: some View {
        let screenSize = UIScreen.main.fixedCoordinateSpace.bounds.size
        return ZStack {
            MirrorPalette.canvas.ignoresSafeArea()
            VStack(spacing: 12) {
                liveCameraPanel
                    .frame(width: 280, height: 400)
                sourceStatus
            }
            if let image = UIImage(named: "mirror-frame-v1") {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFill()
                    .frame(width: screenSize.width, height: screenSize.height)
                    .clipped()
                    .allowsHitTesting(false)
            }
        }
        .frame(width: screenSize.width, height: screenSize.height)
        .ignoresSafeArea()
        .overlay(alignment: .bottom) {
            cameraButton.padding(.bottom, 42)
        }
    }

    private var frozenView: some View {
        ZStack {
            MirrorPalette.canvas.ignoresSafeArea()
            if let image = UIImage(named: "mirror-frame-v1") {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFill()
                    .ignoresSafeArea()
                    .allowsHitTesting(false)
            }
            VStack(spacing: 12) {
                ZStack {
                    frozenPhoto
                    if model.stage == .analyzing { AnalysisLoader() }
                }
                .frame(width: 280, height: 400)
                .clipShape(RoundedRectangle(cornerRadius: 36))
                .overlay(RoundedRectangle(cornerRadius: 36).stroke(MirrorPalette.ink, lineWidth: 2.5))

                Text(model.stage == .analyzing ? "正在分析镜头里的妆效" : "点击按钮或长按照片查看建议")
                    .font(.headline).foregroundStyle(MirrorPalette.ink)
                Text(model.stage == .analyzing ? "请稍等一下" : "会给你很短、可以马上执行的建议")
                    .font(.subheadline).foregroundStyle(MirrorPalette.muted)
                if model.stage != .analyzing {
                    Text(model.result)
                        .font(.caption)
                        .foregroundStyle(MirrorPalette.muted)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 24)
                }
                if model.stage != .analyzing {
                    Button("开始外貌check") { model.startAnalysis() }
                        .buttonStyle(MinimalButtonStyle(filled: true))
                    Button("重新拍摄") { model.returnToLive() }
                        .buttonStyle(MinimalButtonStyle())
                }
            }
            .padding(.bottom, 28)
        }
        .ignoresSafeArea()
    }

    private var frozenPhoto: some View {
        ZStack {
            MirrorPalette.ink.ignoresSafeArea()
            if let image = model.frozenImage {
                Image(uiImage: image).resizable().scaledToFill().ignoresSafeArea()
            } else {
                ContentUnavailableView("没有可分析的画面", systemImage: "camera")
            }
        }
        .contentShape(Rectangle())
        .onLongPressGesture(minimumDuration: 0.55) { model.startAnalysis() }
    }

    private var adviceView: some View {
        GeometryReader { proxy in
            let cardWidth = min(350, min(proxy.size.width - 28, (proxy.size.height - 175) / 1.5))
            VStack(spacing: 0) {
                topBar
                Spacer(minLength: 12)
                Text("妆效建议").font(.system(size: 28, weight: .medium, design: .serif)).foregroundStyle(MirrorPalette.ink)
                Text(model.roadshowLabel ?? "根据刚才的镜头画面")
                    .font(.caption)
                    .foregroundStyle(MirrorPalette.muted)
                    .padding(.top, 4)
                Spacer(minLength: 14)
                if let card = model.currentAdviceCard {
                    adviceCard(card, width: cardWidth)
                        .rotation3DEffect(
                            .degrees(cardFlipAngle),
                            axis: (x: 0, y: 1, z: 0),
                            perspective: 0.7
                        )
                        .overlay(alignment: .leading) {
                            if model.currentCardIndex > 0 { arrowButton("chevron.left") { flipCard(direction: -1) }.offset(x: -19) }
                        }
                        .overlay(alignment: .trailing) {
                            if model.currentCardIndex < model.adviceCards.count - 1 { arrowButton("chevron.right") { flipCard(direction: 1) }.offset(x: 19) }
                        }
                        .overlay(alignment: .bottomTrailing) {
                            Button { model.generateMakeupEffect() } label: {
                                Image(systemName: "flask.fill")
                                    .font(.system(size: 18, weight: .semibold))
                                    .foregroundStyle(MirrorPalette.paper)
                                    .frame(width: 46, height: 46)
                                    .background(MirrorPalette.ochre)
                                    .clipShape(Circle())
                            }
                            .padding(18)
                        }
                        .simultaneousGesture(DragGesture(minimumDistance: 24).onEnded { value in
                            if value.translation.width < -42 { flipCard(direction: 1) }
                            if value.translation.width > 42 { flipCard(direction: -1) }
                        })
                } else {
                    ContentUnavailableView("还没有建议", systemImage: "text.bubble")
                        .frame(width: cardWidth, height: cardWidth * 1.5)
                }
                Spacer(minLength: 12)
                Text("这是最后的建议哦～")
                    .font(.caption)
                    .foregroundStyle(MirrorPalette.muted)
                    .frame(height: 18)
                    .opacity(model.currentCardIndex == model.adviceCards.count - 1 && !model.adviceCards.isEmpty ? 1 : 0)
                HStack(spacing: 14) {
                    Button("回到镜头") { model.returnToLive() }.buttonStyle(MinimalButtonStyle())
                }.padding(.top, 12)
            }
            .frame(width: proxy.size.width, height: proxy.size.height)
        }
        .padding(.horizontal, 14).padding(.top, 10)
    }

    private func flipCard(direction: Int) {
        guard !isCardFlipping else { return }
        let targetIndex = model.currentCardIndex + direction
        guard model.adviceCards.indices.contains(targetIndex) else { return }

        isCardFlipping = true
        withAnimation(.easeIn(duration: 0.2)) {
            cardFlipAngle = direction > 0 ? -90 : 90
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
            model.currentCardIndex = targetIndex
            withAnimation(.easeOut(duration: 0.2)) {
                cardFlipAngle = 0
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                isCardFlipping = false
            }
        }
    }

    private var effectView: some View {
        VStack(spacing: 0) {
            topBar
            Spacer(minLength: 24)
            ZStack {
                cameraFrame(image: model.generatedImage ?? model.frozenImage, placeholder: "效果图生成中…")
                if model.isGenerating { AnalysisLoader(label: "外貌check") }
            }
            Spacer(minLength: 20)
            Text(model.roadshowLabel ?? model.result)
                .font(.caption)
                .foregroundStyle(MirrorPalette.muted)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 18)
            if model.generatedImage != nil {
                Button("保存到照片") { model.saveGeneratedImage() }
                    .buttonStyle(MinimalButtonStyle(filled: true)).padding(.top, 14)
            }
            Button("回到建议") { model.stage = .advice }
                .buttonStyle(MinimalButtonStyle()).padding(.top, 10)
            Spacer(minLength: 24)
        }
        .padding(.horizontal, 22).padding(.top, 10)
    }

    private var topBar: some View {
        HStack {
            Text("LIVE").font(.caption2.weight(.semibold)).tracking(2).foregroundStyle(MirrorPalette.muted)
            Spacer()
            Circle().fill(model.connectionText == "已连接" ? MirrorPalette.ochre : MirrorPalette.muted).frame(width: 7, height: 7)
            Text(model.connectionText).font(.caption).foregroundStyle(MirrorPalette.muted)
        }
    }

    private var liveCameraPanel: some View {
        ZStack(alignment: .topTrailing) {
            RoundedRectangle(cornerRadius: 36).fill(Color(red: 0.62, green: 0.80, blue: 0.95))
            if model.cameraSource == .phone {
                PhoneCameraPreview(session: model.phoneCamera.session)
                    .clipShape(RoundedRectangle(cornerRadius: 36))
            } else if let image = model.hardwareImage {
                Image(uiImage: image).resizable().scaledToFill().clipShape(RoundedRectangle(cornerRadius: 36))
            } else {
                Label("等待镜头画面", systemImage: "video")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(MirrorPalette.ink.opacity(0.62))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(RoundedRectangle(cornerRadius: 36).stroke(MirrorPalette.ink, lineWidth: 2.5))
        .clipped()
    }

    private var sourceStatus: some View {
        HStack(spacing: 6) {
            Circle().fill(model.connectionText == "已连接" ? MirrorPalette.ochre : MirrorPalette.muted).frame(width: 6, height: 6)
            Text(model.connectionText).font(.caption).foregroundStyle(MirrorPalette.muted)
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(MirrorPalette.paper.opacity(0.8))
        .clipShape(Capsule())
    }

    private var cameraButton: some View {
        Image(systemName: "camera").font(.system(size: 22, weight: .medium)).foregroundStyle(MirrorPalette.ochre)
            .frame(width: 58, height: 58).background(MirrorPalette.paper).clipShape(Circle())
            .overlay(Circle().stroke(MirrorPalette.ochre.opacity(0.25), lineWidth: 1))
            .contentShape(Circle())
            .accessibilityAddTraits(.isButton)
            .gesture(
                LongPressGesture(minimumDuration: 0.45)
                    .onEnded { _ in withAnimation(.easeOut(duration: 0.18)) { showSourceMenu.toggle() } }
                    .exclusively(before: TapGesture().onEnded { model.freezeCurrentFrame() })
            )
        .overlay(alignment: .trailing) {
            if showSourceMenu {
                cameraSourceRail
                    .offset(x: -68)
                    .transition(.move(edge: .trailing).combined(with: .opacity))
            }
        }
    }

    private var cameraSourceRail: some View {
        HStack(spacing: 9) {
            ForEach(CameraSource.userSelectable) { source in
                Button {
                    model.selectCameraSource(source)
                    withAnimation(.easeOut(duration: 0.18)) { showSourceMenu = false }
                } label: {
                    VStack(spacing: 5) {
                        Image(systemName: source.icon)
                            .font(.system(size: 16, weight: .medium))
                        Text(source.title)
                            .font(.system(size: 10, weight: .medium))
                            .lineLimit(1)
                    }
                    .foregroundStyle(source == model.cameraSource ? MirrorPalette.paper : MirrorPalette.ochre)
                    .frame(width: 62, height: 58)
                    .background(source == model.cameraSource ? MirrorPalette.ochre : MirrorPalette.paper)
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .overlay(RoundedRectangle(cornerRadius: 16).stroke(MirrorPalette.ochre.opacity(0.28), lineWidth: 1))
                }
            }
        }
        .padding(8)
        .background(MirrorPalette.canvas.opacity(0.94))
        .clipShape(RoundedRectangle(cornerRadius: 22))
        .shadow(color: .black.opacity(0.12), radius: 12, y: 6)
    }

    private func cameraFrame(image: UIImage?, placeholder: String) -> some View {
        ZStack {
            RoundedRectangle(cornerRadius: 28).fill(MirrorPalette.paper)
            if let image {
                // The generated result is shown in full. Do not crop it into
                // the portrait window, otherwise the apparent aspect ratio
                // changes and the face can run outside the intended frame.
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
                    .padding(4)
            }
            else { ContentUnavailableView(placeholder, systemImage: "camera") }
        }
        .frame(maxWidth: .infinity).aspectRatio(0.78, contentMode: .fit)
        .overlay(RoundedRectangle(cornerRadius: 28).stroke(MirrorPalette.ink.opacity(0.12), lineWidth: 1))
        .clipped()
    }

    private func adviceCard(_ card: AdviceCard, width: CGFloat) -> some View {
        VStack(spacing: 0) {
            markdownText(card.title)
                .font(.system(size: 24, weight: .bold, design: .serif))
                .foregroundStyle(MirrorPalette.ink)
                .multilineTextAlignment(.center)
                .lineLimit(2)
                .frame(maxWidth: .infinity)
                .padding(.top, 52)
                .padding(.bottom, 24)
            ScrollView(.vertical, showsIndicators: true) {
                VStack(alignment: .leading, spacing: 16) {
                    ForEach(card.bullets, id: \.self) { bullet in
                        HStack(alignment: .top, spacing: 10) {
                            Circle().fill(MirrorPalette.ochre).frame(width: 5, height: 5).padding(.top, 8)
                            markdownText(bullet)
                                .font(.system(size: 16))
                                .foregroundStyle(MirrorPalette.ink)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .padding(.bottom, 34)
            }
            .padding(.horizontal, 34)
        }
        .frame(width: width, height: width * 1.5)
        .background {
            if let image = UIImage(named: "advice-card-bg-user-v1") {
                // The supplied artwork contains a thin white export margin.
                // Slightly overfill it so the card edge is the artwork itself.
                Image(uiImage: image).resizable().scaledToFill().scaleEffect(1.04)
            } else {
                MirrorPalette.paper
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 24))
        .overlay(RoundedRectangle(cornerRadius: 24).stroke(MirrorPalette.ink.opacity(0.1), lineWidth: 1))
        .shadow(color: .black.opacity(0.08), radius: 14, y: 8)
    }

    private func markdownText(_ value: String) -> Text {
        if let attributed = try? AttributedString(markdown: value) {
            return Text(attributed)
        }
        return Text(value)
    }

    private func arrowButton(_ systemName: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: systemName).foregroundStyle(MirrorPalette.ochre).frame(width: 38, height: 38).background(MirrorPalette.paper).clipShape(Circle()) }
    }
}

private struct AnalysisLoader: View {
    let label: String
    @State private var rotation = 0.0

    init(label: String = "外貌 check ～") {
        self.label = label
    }

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "arrow.triangle.2.circlepath.circle.fill")
                .font(.system(size: 88, weight: .light))
                .foregroundStyle(MirrorPalette.paper)
                .shadow(color: .black.opacity(0.35), radius: 12)
                .rotationEffect(.degrees(rotation))
            Text(label)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(MirrorPalette.paper)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(.black.opacity(0.38), in: Capsule())
        }
        .onAppear {
            rotation = 360
        }
        .onDisappear {
            rotation = 0
        }
        .animation(.linear(duration: 1.0).repeatForever(autoreverses: false), value: rotation)
    }
}

private struct PhoneCameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.videoPreviewLayer.session = session
        view.videoPreviewLayer.videoGravity = .resizeAspectFill
        view.updateVideoOrientation()
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {
        uiView.videoPreviewLayer.session = session
        uiView.updateVideoOrientation()
    }

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var videoPreviewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }

        func updateVideoOrientation() {
            guard let connection = videoPreviewLayer.connection else { return }
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
    }
}

private struct MinimalButtonStyle: ButtonStyle {
    var filled = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.subheadline.weight(.semibold)).foregroundStyle(filled ? MirrorPalette.paper : MirrorPalette.ochre)
            .padding(.horizontal, 16).padding(.vertical, 11)
            .background(filled ? MirrorPalette.ochre : MirrorPalette.paper).clipShape(Capsule())
            .overlay(Capsule().stroke(MirrorPalette.ochre.opacity(filled ? 0 : 0.3), lineWidth: 1)).opacity(configuration.isPressed ? 0.7 : 1)
    }
}

#Preview { ContentView().environmentObject(KanjianViewModel()) }
