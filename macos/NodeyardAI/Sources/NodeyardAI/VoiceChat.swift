@preconcurrency import AVFoundation
import Foundation
@preconcurrency import Speech

/// Voice chat: talk to the model like a phone call. It listens (Apple's speech recognition, on this Mac when it
/// can), sends what you said when you pause, reads the answer aloud while it's written, then listens again.
@MainActor
final class VoiceChat: ObservableObject {
    static let shared = VoiceChat()

    enum Phase: Equatable { case off, listening, thinking, speaking }
    @Published private(set) var phase: Phase = .off
    @Published private(set) var heard = ""

    private weak var state: AppState?
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var silence: Task<Void, Never>?
    private var watch: Task<Void, Never>?
    private var waiting = false   // sent, the answer hasn't finished yet

    var isOn: Bool { phase != .off }

    func toggle(_ state: AppState) {
        switch phase {
        case .off: Task { await start(state) }
        case .speaking: SpeechReader.shared.stop(); listen()                 // talk over it: it stops and listens
        case .listening where !heard.isEmpty: sendHeard()                    // send now
        default: stop()
        }
    }

    func start(_ state: AppState) async {
        self.state = state
        guard await Self.authorize() else {
            state.error = "Voice chat needs the microphone and speech recognition: allow Nodeyard AI in System Settings › Privacy & Security."
            return
        }
        SpeechReader.shared.stop()
        phase = .listening
        watch?.cancel()
        watch = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 400_000_000)
                self?.tick()
            }
        }
        listen()
    }

    func stop() {
        watch?.cancel(); watch = nil
        stopListening()
        SpeechReader.shared.stop()
        if !heard.isEmpty, state?.composer == heard { state?.composer = "" }
        heard = ""
        waiting = false
        phase = .off
    }

    private static func authorize() async -> Bool {
        let speech: Bool = await withCheckedContinuation { c in
            SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0 == .authorized) }
        }
        guard speech else { return false }
        return await AVCaptureDevice.requestAccess(for: .audio)
    }

    /// What to do next: listen again once the answer has been written and read out.
    private func tick() {
        guard isOn, let state else { return }
        let reading = SpeechReader.shared.busy
        if waiting || state.isSending {
            phase = reading ? .speaking : .thinking
            if !state.isSending && waiting { waiting = false }
            return
        }
        if reading { phase = .speaking; return }
        if task == nil { listen() }
    }

    private func listen() {
        guard isOn else { return }
        stopListening()
        let locale = Locale(identifier: Locale.preferredLanguages.first ?? "en-US")
        guard let recognizer = SFSpeechRecognizer(locale: locale) ?? SFSpeechRecognizer(), recognizer.isAvailable else {
            state?.error = "Speech recognition isn't available on this Mac right now."
            stop()
            return
        }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        if recognizer.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }   // stays on this Mac
        let input = engine.inputNode
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0), block: Self.tap(req))
        engine.prepare()
        do { try engine.start() } catch {
            state?.error = "The microphone didn't start: \(error.localizedDescription)"
            stop()
            return
        }
        request = req
        heard = ""
        phase = .listening
        task = recognizer.recognitionTask(with: req, resultHandler: Self.results { [weak self] text, final, failed in
            self?.got(text, final: final, failed: failed)
        })
    }

    private func stopListening() {
        silence?.cancel(); silence = nil
        task?.cancel(); task = nil
        request?.endAudio(); request = nil
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
    }

    private func got(_ text: String?, final: Bool, failed: Bool) {
        guard isOn, phase == .listening else { return }
        if let text, !text.isEmpty {
            heard = text
            state?.composer = text
            silence?.cancel()
            silence = Task { [weak self] in   // a pause: you've finished
                try? await Task.sleep(nanoseconds: 1_400_000_000)
                if !Task.isCancelled { self?.sendHeard() }
            }
        }
        if failed || final {   // the recognizer gave up (long silence, or an error): start a fresh one
            if !heard.isEmpty { sendHeard() } else { stopListening() }   // (tick listens again)
        }
    }

    private func sendHeard() {
        guard let state, isOn else { return }
        let text = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        stopListening()
        heard = ""
        guard !text.isEmpty, !state.isSending else { return }
        state.composer = text
        waiting = true
        phase = .thinking
        state.beginSend()
    }

    // Built outside the main actor: the audio and recognition callbacks run on their own threads.
    private nonisolated static func tap(_ req: SFSpeechAudioBufferRecognitionRequest) -> AVAudioNodeTapBlock {
        nonisolated(unsafe) let request = req
        return { buffer, _ in request.append(buffer) }
    }

    private nonisolated static func results(_ deliver: @escaping @MainActor (String?, Bool, Bool) -> Void) -> (SFSpeechRecognitionResult?, Error?) -> Void {
        return { result, error in
            let text = result?.bestTranscription.formattedString
            let final = result?.isFinal ?? false
            let failed = error != nil
            Task { @MainActor in deliver(text, final, failed) }
        }
    }
}
