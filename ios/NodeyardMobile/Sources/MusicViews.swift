import SwiftUI

struct MusicView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var player: MusicPlayer
    @StateObject private var lib = MusicLibrary()
    @State private var section = Section.liked
    @State private var query = ""
    @State private var searchTask: Task<Void, Never>?
    @State private var newPlaylist = false
    @State private var newName = ""

    enum Section: String, CaseIterable { case liked = "Liked", library = "Library", playlists = "Playlists" }

    var body: some View {
        NavigationStack {
            List {
                if !query.isEmpty {
                    searchResults
                } else {
                    Picker("Section", selection: $section) {
                        ForEach(Section.allCases, id: \.self) { Text($0.rawValue) }
                    }
                    .pickerStyle(.segmented)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 8, trailing: 16))
                    if let p = lib.problem { ErrorBanner(text: p).listRowBackground(Color.clear) }
                    switch section {
                    case .liked: tracks(lib.likes, empty: ("heart", "No liked songs yet", "Search SoundCloud above and tap the heart."))
                    case .library: tracks(lib.library, empty: ("folder", "Library is empty", "Set a music folder in the dashboard's Music page."))
                    case .playlists: playlistRows
                    }
                }
            }
            .listStyle(.insetGrouped)
            .screen()
            .navigationTitle("Music")
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Search SoundCloud")
            .onChange(of: query) { q in
                searchTask?.cancel()
                searchTask = Task {
                    try? await Task.sleep(nanoseconds: 450_000_000)
                    guard !Task.isCancelled else { return }
                    await lib.search(q)
                }
            }
            .refreshable { await lib.load() }
            .toolbar {
                if section == .playlists && query.isEmpty {
                    Button { newName = ""; newPlaylist = true } label: { Image(systemName: "plus") }
                        .accessibilityLabel("New playlist")
                }
            }
            .alert("New playlist", isPresented: $newPlaylist) {
                TextField("Name", text: $newName)
                Button("Cancel", role: .cancel) {}
                Button("Create") { Task { await lib.playlistOp(["action": "create", "name": newName]) } }
            }
            .navigationDestination(for: PlaylistInfo.self) { PlaylistView(info: $0) }
        }
        .environmentObject(lib)
        .task {
            lib.api = model.api
            if !lib.loaded { await lib.load() }
        }
    }

    @ViewBuilder private var searchResults: some View {
        if lib.searching && lib.results.isEmpty {
            HStack { Spacer(); ProgressView(); Spacer() }.listRowBackground(Color.clear)
        } else if !lib.searchReady {
            EmptyNote(symbol: "magnifyingglass", title: "Search isn't set up", detail: "The server needs yt-dlp to search SoundCloud.")
                .listRowBackground(Color.clear)
        } else {
            tracks(lib.results, empty: ("magnifyingglass", "No results", "Try another song or artist."))
        }
    }

    @ViewBuilder
    private func tracks(_ list: [Track], empty: (String, String, String)) -> some View {
        if list.isEmpty && lib.loaded {
            EmptyNote(symbol: empty.0, title: empty.1, detail: empty.2).listRowBackground(Color.clear)
        } else if list.isEmpty {
            HStack { Spacer(); ProgressView(); Spacer() }.listRowBackground(Color.clear)
        } else {
            ForEach(Array(list.enumerated()), id: \.element.id) { i, t in
                TrackRow(track: t) { player.play(list, at: i) }
            }
        }
    }

    @ViewBuilder private var playlistRows: some View {
        if lib.playlists.isEmpty {
            EmptyNote(symbol: "music.note.list", title: "No playlists", detail: "Tap + to make one, then add songs from their menu.")
                .listRowBackground(Color.clear)
        } else {
            ForEach(lib.playlists) { p in
                NavigationLink(value: p) {
                    HStack(spacing: 14) {
                        RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Theme.brand)
                            .frame(width: 48, height: 48)
                            .overlay(Image(systemName: "music.note.list").foregroundStyle(.white))
                        VStack(alignment: .leading, spacing: 3) {
                            Text(p.name ?? "Playlist").font(.system(size: 16, weight: .semibold, design: .rounded))
                            Text("\(p.count ?? 0) songs").font(.system(size: 13)).foregroundStyle(Theme.mist)
                        }
                    }
                }
                .rowStyle()
                .swipeActions {
                    Button(role: .destructive) { Task { await lib.playlistOp(["action": "delete", "id": p.id]) } } label: {
                        Label("Delete", systemImage: "trash")
                    }
                }
            }
        }
    }
}

struct TrackRow: View {
    let track: Track
    var removable: (() -> Void)? = nil
    let play: () -> Void
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var player: MusicPlayer
    @EnvironmentObject private var lib: MusicLibrary

    var body: some View {
        let isCurrent = player.current?.id == track.id
        Button(action: play) {
            HStack(spacing: 14) {
                Artwork(track: track, size: 48)
                VStack(alignment: .leading, spacing: 3) {
                    Text(track.displayTitle)
                        .font(.system(size: 16, weight: .semibold, design: .rounded))
                        .foregroundStyle(isCurrent ? Theme.teal : .white)
                        .lineLimit(1)
                    Text(track.subtitle).font(.system(size: 13)).foregroundStyle(Theme.mist).lineLimit(1)
                }
                Spacer(minLength: 8)
                if isCurrent && player.playing {
                    Image(systemName: "waveform").foregroundStyle(Theme.teal).symbolEffectIfAvailable()
                } else if let d = track.duration {
                    Text(Format.clock(d)).font(.system(size: 13).monospacedDigit()).foregroundStyle(Theme.mist)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .rowStyle()
        .contextMenu { menu }
        .swipeActions(edge: .leading) {
            Button { Task { await lib.toggleLike(track) } } label: {
                Label(lib.isLiked(track) ? "Unlike" : "Like", systemImage: lib.isLiked(track) ? "heart.slash" : "heart")
            }
            .tint(Theme.indigo)
        }
        .swipeActions(edge: .trailing) {
            if let removable {
                Button(role: .destructive, action: removable) { Label("Remove", systemImage: "minus.circle") }
            }
        }
    }

    @ViewBuilder private var menu: some View {
        Button { Task { await lib.toggleLike(track) } } label: {
            Label(lib.isLiked(track) ? "Unlike" : "Like", systemImage: lib.isLiked(track) ? "heart.slash" : "heart")
        }
        if !lib.playlists.isEmpty {
            Menu {
                ForEach(lib.playlists) { p in
                    Button(p.name ?? "Playlist") { Task { await lib.playlistOp(["action": "add", "id": p.id, "track": track.payload]) } }
                }
            } label: { Label("Add to playlist", systemImage: "text.badge.plus") }
        }
        if let removable {
            Button(role: .destructive, action: removable) { Label("Remove from playlist", systemImage: "minus.circle") }
        }
    }
}

private extension View {
    @ViewBuilder func symbolEffectIfAvailable() -> some View {
        if #available(iOS 17, *) { self.symbolEffect(.variableColor.iterative) } else { self }
    }
}

struct Artwork: View {
    let track: Track?
    var size: CGFloat
    @EnvironmentObject private var model: AppModel
    var body: some View {
        RemoteImage(url: track.flatMap { t in model.api.flatMap { t.artURL($0) } }, api: model.api) {
            ZStack {
                Theme.brand.opacity(0.55)
                Image(systemName: "music.note").font(.system(size: size * 0.38, weight: .semibold)).foregroundStyle(.white.opacity(0.9))
            }
        }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: size * 0.2, style: .continuous))
    }
}

struct PlaylistView: View {
    let info: PlaylistInfo
    @EnvironmentObject private var lib: MusicLibrary
    @EnvironmentObject private var player: MusicPlayer
    @State private var full: PlaylistInfo?
    @State private var renaming = false
    @State private var name = ""

    var body: some View {
        let tracks = full?.tracks ?? []
        List {
            if full == nil {
                HStack { Spacer(); ProgressView(); Spacer() }.listRowBackground(Color.clear)
            } else if tracks.isEmpty {
                EmptyNote(symbol: "music.note.list", title: "Empty playlist", detail: "Long-press any song and choose Add to playlist.")
                    .listRowBackground(Color.clear)
            } else {
                Button { player.play(tracks.shuffled(), at: 0) } label: {
                    Label("Shuffle", systemImage: "shuffle")
                }
                .buttonStyle(BrandButtonStyle())
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets())
                ForEach(Array(tracks.enumerated()), id: \.offset) { i, t in
                    TrackRow(track: t, removable: { remove(i) }) { player.play(tracks, at: i) }
                }
            }
        }
        .listStyle(.insetGrouped)
        .screen()
        .navigationTitle(full?.name ?? info.name ?? "Playlist")
        .toolbar {
            Button { name = full?.name ?? info.name ?? ""; renaming = true } label: { Image(systemName: "pencil") }
                .accessibilityLabel("Rename")
        }
        .alert("Rename playlist", isPresented: $renaming) {
            TextField("Name", text: $name)
            Button("Cancel", role: .cancel) {}
            Button("Save") { Task { await lib.playlistOp(["action": "rename", "id": info.id, "name": name]); await reload() } }
        }
        .task { await reload() }
        .refreshable { await reload() }
    }

    private func reload() async { full = await lib.playlist(info.id) ?? full }

    private func remove(_ i: Int) {
        full?.tracks?.remove(at: i)
        Task { await lib.playlistOp(["action": "remove", "id": info.id, "index": i]); await reload() }
    }
}

// MARK: - Player

/// The bar above the tab bar while something is playing.
struct MiniPlayer: View {
    @EnvironmentObject private var player: MusicPlayer
    @State private var open = false

    var body: some View {
        if let t = player.current {
            HStack(spacing: 12) {
                Artwork(track: t, size: 42)
                VStack(alignment: .leading, spacing: 2) {
                    Text(t.displayTitle).font(.system(size: 15, weight: .semibold, design: .rounded)).lineLimit(1)
                    Text(t.subtitle).font(.system(size: 12)).foregroundStyle(Theme.mist).lineLimit(1)
                }
                Spacer(minLength: 4)
                Button { player.toggle() } label: {
                    Group {
                        if player.buffering { ProgressView().tint(.white) }
                        else { Image(systemName: player.playing ? "pause.fill" : "play.fill") }
                    }
                    .font(.system(size: 20)).frame(width: 44, height: 44)
                }
                .accessibilityLabel(player.playing ? "Pause" : "Play")
                Button { player.next() } label: {
                    Image(systemName: "forward.fill").font(.system(size: 18)).frame(width: 40, height: 44)
                }
                .disabled(!player.hasNext)
                .accessibilityLabel("Next")
            }
            .foregroundStyle(.white)
            .padding(.leading, 8)
            .padding(.trailing, 6)
            .padding(.vertical, 6)
            .background(alignment: .bottom) {
                ZStack(alignment: .bottomLeading) {
                    RoundedRectangle(cornerRadius: 18, style: .continuous).fill(.ultraThinMaterial)
                    RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Theme.panel.opacity(0.7))
                    GeometryReader { g in
                        Capsule().fill(Theme.brand).frame(width: g.size.width * player.progress, height: 2)
                            .frame(maxHeight: .infinity, alignment: .bottom)
                    }
                    .padding(.horizontal, 14)
                }
            }
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(Theme.line))
            .shadow(color: .black.opacity(0.35), radius: 16, y: 6)
            .padding(.horizontal, 10)
            .padding(.bottom, 6)
            .contentShape(Rectangle())
            .onTapGesture { open = true }
            .sheet(isPresented: $open) { FullPlayer() }
            .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }
}

struct FullPlayer: View {
    @EnvironmentObject private var player: MusicPlayer
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var scrub: Double?
    @StateObject private var likes = MusicLibrary()

    var body: some View {
        let t = player.current
        ZStack {
            Theme.ink.ignoresSafeArea()
            if let art = player.artwork {
                Image(uiImage: art).resizable().scaledToFill().blur(radius: 60).opacity(0.45).ignoresSafeArea()
            }
            VStack(spacing: 0) {
                Capsule().fill(Color.white.opacity(0.3)).frame(width: 40, height: 5).padding(.top, 10)
                Spacer(minLength: 20)
                Artwork(track: t, size: 300)
                    .shadow(color: .black.opacity(0.5), radius: 30, y: 16)
                    .scaleEffect(player.playing ? 1 : 0.9)
                    .animation(.spring(response: 0.5, dampingFraction: 0.7), value: player.playing)
                Spacer(minLength: 20)
                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(t?.displayTitle ?? "").font(Theme.title(22)).lineLimit(2)
                        Text(t?.subtitle ?? "").font(.system(size: 16, design: .rounded)).foregroundStyle(Theme.mist).lineLimit(1)
                    }
                    Spacer()
                    if let t {
                        Button { Task { await likes.toggleLike(t) } } label: {
                            Image(systemName: likes.isLiked(t) ? "heart.fill" : "heart")
                                .font(.system(size: 24)).foregroundStyle(likes.isLiked(t) ? Theme.teal : .white)
                        }
                        .accessibilityLabel(likes.isLiked(t) ? "Unlike" : "Like")
                    }
                }
                .padding(.horizontal, 28)

                VStack(spacing: 6) {
                    Slider(value: Binding(get: { scrub ?? player.elapsed }, set: { scrub = $0 }),
                           in: 0...max(1, player.duration)) { editing in
                        if !editing, let s = scrub { player.seek(s); scrub = nil }
                    }
                    .tint(Theme.teal)
                    HStack {
                        Text(Format.clock(scrub ?? player.elapsed))
                        Spacer()
                        Text("-" + Format.clock(max(0, player.duration - (scrub ?? player.elapsed))))
                    }
                    .font(.system(size: 12, design: .rounded).monospacedDigit())
                    .foregroundStyle(Theme.mist)
                }
                .padding(.horizontal, 28)
                .padding(.top, 22)

                if let p = player.problem {
                    Text(p).font(.system(size: 13)).foregroundStyle(Theme.danger).padding(.horizontal, 28).padding(.top, 6)
                }

                HStack(spacing: 44) {
                    Button { player.repeatOne.toggle(); Haptics.tap() } label: {
                        Image(systemName: "repeat.1").font(.system(size: 20))
                            .foregroundStyle(player.repeatOne ? Theme.teal : Theme.mist)
                    }
                    .accessibilityLabel("Repeat one")
                    Button { player.previous() } label: { Image(systemName: "backward.fill").font(.system(size: 30)) }
                        .accessibilityLabel("Previous")
                    Button { player.toggle() } label: {
                        ZStack {
                            Circle().fill(.white).frame(width: 76, height: 76)
                            if player.buffering { ProgressView().tint(Theme.ink) }
                            else {
                                Image(systemName: player.playing ? "pause.fill" : "play.fill")
                                    .font(.system(size: 32)).foregroundStyle(Theme.ink)
                                    .offset(x: player.playing ? 0 : 2)
                            }
                        }
                    }
                    .accessibilityLabel(player.playing ? "Pause" : "Play")
                    Button { player.next() } label: { Image(systemName: "forward.fill").font(.system(size: 30)) }
                        .disabled(!player.hasNext)
                        .accessibilityLabel("Next")
                    Color.clear.frame(width: 20, height: 20)
                }
                .foregroundStyle(.white)
                .padding(.top, 28)
                Spacer(minLength: 30)
            }
        }
        .preferredColorScheme(.dark)
        .presentationDragIndicator(.hidden)
        .task { likes.api = model.api; await likes.load() }
    }
}
