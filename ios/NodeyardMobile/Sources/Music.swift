import AVFoundation
import MediaPlayer
import SwiftUI

/// A song: from SoundCloud (has `url`) or from the music folder on the server (has `path`).
struct Track: Codable, Hashable, Identifiable {
    var kind: String?
    var url: String?
    var path: String?
    var title: String?
    var artist: String?
    var name: String?
    var folder: String?
    var duration_ms: Double?
    var artwork_url: String?
    var size: Double?

    var id: String { url ?? path ?? UUID().uuidString }
    var isLocal: Bool { path != nil && url == nil }
    var displayTitle: String {
        if let title, !title.isEmpty { return title }
        let n = name ?? path ?? "Untitled"
        return ((n as NSString).lastPathComponent as NSString).deletingPathExtension
    }
    var subtitle: String {
        if let artist, !artist.isEmpty { return artist }
        if let folder, !folder.isEmpty { return folder }
        return isLocal ? "Music library" : "SoundCloud"
    }
    var duration: Double? { duration_ms.flatMap { $0 > 0 ? $0 / 1000 : nil } }

    /// What the server wants back when this track is liked or put in a playlist.
    var payload: [String: Any] {
        var d: [String: Any] = [:]
        if let url { d["url"] = url; d["kind"] = "sc" } else if let path { d["path"] = path; d["kind"] = "local" }
        if let title { d["title"] = title }
        if let artist { d["artist"] = artist }
        if let name { d["name"] = name }
        if let folder { d["folder"] = folder }
        if let duration_ms { d["duration_ms"] = Int(duration_ms) }
        if let artwork_url { d["artwork_url"] = artwork_url }
        return d
    }

    func streamURL(_ api: NodeyardAPI) -> URL {
        if let url { return api.url("/api/music/sc-stream", ["url": url]) }
        return api.url("/api/music/track", ["path": path ?? ""])
    }

    func artURL(_ api: NodeyardAPI) -> URL? {
        if let art = artwork_url, !art.isEmpty { return api.url("/api/music/art", ["url": art]) }
        if let url { return api.url("/api/music/art", ["track": url]) }
        if let path { return api.url("/api/music/art", ["path": path]) }
        return nil
    }
}

struct PlaylistInfo: Decodable, Identifiable, Hashable {
    var id: String
    var name: String?
    var count: Int?
    var created: Double?
    var tracks: [Track]?
}

private struct MusicReply: Decodable {
    var tracks: [Track]?
    var likes: [Track]?
    var playlists: [PlaylistInfo]?
    var sc: SC?
    struct SC: Decodable { var search_ready: Bool?; var stream_ready: Bool? }
}
private struct SearchReply: Decodable { var tracks: [Track]? }
private struct LikesReply: Decodable { var likes: [Track]? }
private struct PlaylistsReply: Decodable { var playlists: [PlaylistInfo]?; var id: String? }
private struct PlaylistReply: Decodable { var playlist: PlaylistInfo? }

/// The music library, likes and playlists the dashboard keeps.
@MainActor
final class MusicLibrary: ObservableObject {
    @Published var library: [Track] = []
    @Published var likes: [Track] = []
    @Published var playlists: [PlaylistInfo] = []
    @Published var results: [Track] = []
    @Published var searching = false
    @Published var loaded = false
    @Published var searchReady = true
    @Published var problem: String?

    var api: NodeyardAPI?

    func load() async {
        guard let api else { return }
        do {
            let r = try await api.get("/api/music", as: MusicReply.self)
            library = (r.tracks ?? []).map { var t = $0; t.kind = "local"; return t }
            likes = r.likes ?? []
            playlists = r.playlists ?? []
            searchReady = r.sc?.search_ready ?? true
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
        loaded = true
    }

    func search(_ q: String) async {
        guard let api else { return }
        let q = q.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { results = []; return }
        searching = true
        defer { searching = false }
        do {
            let r = try await api.get("/api/music/search", ["q": q], as: SearchReply.self)
            guard !Task.isCancelled else { return }
            results = r.tracks ?? []
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    func isLiked(_ t: Track) -> Bool { likes.contains { $0.id == t.id } }

    func toggleLike(_ t: Track) async {
        guard let api else { return }
        let liked = !isLiked(t)
        if liked { likes.insert(t, at: 0) } else { likes.removeAll { $0.id == t.id } }
        Haptics.tap()
        do {
            let r = try await api.post("/api/music/like", ["track": t.payload, "liked": liked], as: LikesReply.self)
            if let fresh = r.likes { likes = fresh }
        } catch {
            problem = error.localizedDescription
            await load()
        }
    }

    func playlist(_ id: String) async -> PlaylistInfo? {
        guard let api else { return nil }
        return try? await api.get("/api/music/playlists", ["id": id], as: PlaylistReply.self).playlist
    }

    @discardableResult
    func playlistOp(_ body: [String: Any]) async -> String? {
        guard let api else { return nil }
        do {
            let r = try await api.post("/api/music/playlist", body, as: PlaylistsReply.self)
            if let p = r.playlists { playlists = p }
            Haptics.success()
            return r.id
        } catch {
            problem = error.localizedDescription
            Haptics.error()
            return nil
        }
    }
}

/// Plays music natively: keeps going with the screen locked and shows on the lock screen and Control Center.
@MainActor
final class MusicPlayer: ObservableObject {
    @Published private(set) var current: Track?
    @Published private(set) var queue: [Track] = []
    @Published private(set) var index = 0
    @Published private(set) var playing = false
    @Published private(set) var buffering = false
    @Published private(set) var elapsed: Double = 0
    @Published private(set) var duration: Double = 0
    @Published private(set) var artwork: UIImage?
    @Published var problem: String?
    @Published var repeatOne = false

    var api: NodeyardAPI?
    private let player = AVPlayer()
    private var timeObserver: Any?
    private var endObserver: NSObjectProtocol?
    private var statusObserver: NSKeyValueObservation?
    private var waitObserver: NSKeyValueObservation?

    init() {
        player.automaticallyWaitsToMinimizeStalling = true
        timeObserver = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 600), queue: .main) { [weak self] t in
            Task { @MainActor in
                guard let self else { return }
                self.elapsed = t.seconds.isFinite ? t.seconds : 0
                if let d = self.player.currentItem?.duration.seconds, d.isFinite, d > 0 { self.duration = d }
            }
        }
        waitObserver = player.observe(\.timeControlStatus, options: [.new]) { [weak self] p, _ in
            let status = p.timeControlStatus
            Task { @MainActor in
                self?.buffering = status == .waitingToPlayAtSpecifiedRate
                self?.playing = status != .paused
                self?.updateNowPlaying()
            }
        }
        setUpRemote()
    }

    var progress: Double { duration > 0 ? min(1, elapsed / duration) : 0 }
    var hasNext: Bool { index + 1 < queue.count }

    func play(_ list: [Track], at i: Int) {
        guard list.indices.contains(i) else { return }
        queue = list
        index = i
        start()
    }

    func toggle() {
        guard current != nil else { return }
        if player.timeControlStatus == .paused { player.play() } else { player.pause() }
        Haptics.tap()
    }

    func next() {
        guard hasNext else { player.pause(); player.seek(to: .zero); return }
        index += 1
        start()
    }

    func previous() {
        if elapsed > 4 || index == 0 { seek(0); return }
        index -= 1
        start()
    }

    func seek(_ seconds: Double) {
        player.seek(to: CMTime(seconds: max(0, seconds), preferredTimescale: 600)) { [weak self] _ in
            Task { @MainActor in self?.updateNowPlaying() }
        }
        elapsed = seconds
    }

    func stop() {
        player.pause()
        player.replaceCurrentItem(with: nil)
        current = nil
        queue = []
        artwork = nil
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
    }

    private func start() {
        guard let api, queue.indices.contains(index) else { return }
        let track = queue[index]
        current = track
        problem = nil
        elapsed = 0
        duration = track.duration ?? 0
        artwork = nil
        var headers = ["X-Nodeyard": "1", "User-Agent": "NodeyardMobile/1.0"]
        let c = api.cookieHeader
        if !c.isEmpty { headers["Cookie"] = c }
        let asset = AVURLAsset(url: track.streamURL(api), options: ["AVURLAssetHTTPHeaderFieldsKey": headers])
        let item = AVPlayerItem(asset: asset)
        item.preferredForwardBufferDuration = 20
        statusObserver = item.observe(\.status, options: [.new]) { [weak self] item, _ in
            let failed = item.status == .failed
            let why = item.error?.localizedDescription
            Task { @MainActor in
                guard failed, let self else { return }
                self.problem = "Couldn't play this track. " + (why ?? "")
                Haptics.error()
            }
        }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        endObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                if self.repeatOne { self.seek(0); self.player.play() } else { self.next() }
            }
        }
        try? AVAudioSession.sharedInstance().setActive(true)
        player.replaceCurrentItem(with: item)
        player.play()
        updateNowPlaying()
        if let art = track.artURL(api) {
            Task {
                let img = await ImageCache.shared.image(art, api: api)
                guard self.current?.id == track.id else { return }
                self.artwork = img
                self.updateNowPlaying()
            }
        }
    }

    // MARK: - Lock screen

    private func updateNowPlaying() {
        guard let t = current else { return }
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: t.displayTitle,
            MPMediaItemPropertyArtist: t.subtitle,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: elapsed,
            MPNowPlayingInfoPropertyPlaybackRate: playing ? 1.0 : 0.0,
        ]
        if duration > 0 { info[MPMediaItemPropertyPlaybackDuration] = duration }
        if let artwork {
            info[MPMediaItemPropertyArtwork] = MPMediaItemArtwork(boundsSize: artwork.size) { _ in artwork }
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    private func setUpRemote() {
        let rc = MPRemoteCommandCenter.shared()
        rc.playCommand.addTarget { [weak self] _ in Task { @MainActor in self?.player.play() }; return .success }
        rc.pauseCommand.addTarget { [weak self] _ in Task { @MainActor in self?.player.pause() }; return .success }
        rc.togglePlayPauseCommand.addTarget { [weak self] _ in Task { @MainActor in self?.toggle() }; return .success }
        rc.nextTrackCommand.addTarget { [weak self] _ in Task { @MainActor in self?.next() }; return .success }
        rc.previousTrackCommand.addTarget { [weak self] _ in Task { @MainActor in self?.previous() }; return .success }
        rc.changePlaybackPositionCommand.addTarget { [weak self] e in
            guard let e = e as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            let at = e.positionTime
            Task { @MainActor in self?.seek(at) }
            return .success
        }
    }
}
