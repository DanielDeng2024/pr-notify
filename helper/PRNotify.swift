// PRNotify: notification helper and menu bar app for pr-notify.
//
//   PRNotify --title T --body B [--url U] [--id ID]   post one notification, exit
//   PRNotify --check                                  request permission, print status
//   PRNotify --menubar --python P --cwd D --interval S --state F
//                                                     status bar app; runs `P -m pr_notify once` every S seconds
//   PRNotify                                          (relaunched by macOS on a notification click)
import AppKit
import UserNotifications

func arg(_ name: String) -> String? {
    let a = CommandLine.arguments
    guard let i = a.firstIndex(of: name), i + 1 < a.count else { return nil }
    return a[i + 1]
}

let center = UNUserNotificationCenter.current()

final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .list, .sound])
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        if let s = r.notification.request.content.userInfo["url"] as? String, let u = URL(string: s) {
            NSWorkspace.shared.open(u)
        }
        done()
        // The menu bar instance stays alive; one-shot instances exit.
        if !CommandLine.arguments.contains("--menubar") { exit(0) }
    }
}

// MARK: - status.json (written by pr_notify.cli.tracked_poll)

struct Item: Decodable { let ref: String; let title: String; let url: String; let text: String; let draft: Bool?; let mine: Bool?
    var needsYou: Bool { !text.isEmpty } }

struct Status: Decodable {
    let last_run: String?
    let last_success: String?
    let ok: Bool
    let error: String?
    let prs: Int
    let notified: Int
    let notify_errors: Int
    let items: [Item]
}

func ago(_ iso: String?) -> String {
    guard let iso = iso, let d = ISO8601DateFormatter().date(from: iso) else { return "never" }
    let s = Int(Date().timeIntervalSince(d))
    if s < 60 { return "just now" }
    if s < 3600 { return "\(s / 60)m ago" }
    if s < 86400 { return "\(s / 3600)h ago" }
    return "\(s / 86400)d ago"
}

// MARK: - Menu bar app

final class MenuBar: NSObject, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    let python = arg("--python") ?? "/usr/bin/python3"
    let cwd = arg("--cwd") ?? NSHomeDirectory()
    let interval = TimeInterval(arg("--interval") ?? "120") ?? 120
    let stateFile = arg("--state") ?? NSHomeDirectory() + "/.local/state/pr-notify/state.json"
    var statusURL: URL { URL(fileURLWithPath: stateFile).deletingLastPathComponent().appendingPathComponent("status.json") }
    var logURL: URL { URL(fileURLWithPath: stateFile).deletingLastPathComponent().appendingPathComponent("app.log") }

    var process: Process?
    var launchError: String?
    var lastStarted: Date?

    func start() {
        menu.delegate = self
        item.menu = menu
        refresh()
        poll()
        Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { _ in self.poll() }
        Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { _ in self.refresh() }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in
            self.poll()
        }
    }

    func poll() {
        if process?.isRunning == true { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-m", "pr_notify", "--state", stateFile, "once"]
        p.currentDirectoryURL = URL(fileURLWithPath: cwd)
        var env = ProcessInfo.processInfo.environment
        // launchd gives a minimal PATH; gh usually lives in Homebrew's prefix.
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
        p.environment = env
        if let log = try? openLog() { p.standardOutput = log; p.standardError = log }
        p.terminationHandler = { _ in DispatchQueue.main.async { self.process = nil; self.refresh() } }
        do {
            try p.run()
            process = p
            launchError = nil
            lastStarted = Date()
            // A hung gh must not wedge the loop.
            DispatchQueue.main.asyncAfter(deadline: .now() + 180) { if p.isRunning { p.terminate() } }
        } catch {
            launchError = "cannot run \(python): \(error.localizedDescription)"
        }
        refresh()
    }

    func openLog() throws -> FileHandle {
        let fm = FileManager.default
        try fm.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !fm.fileExists(atPath: logURL.path) { fm.createFile(atPath: logURL.path, contents: nil) }
        let h = try FileHandle(forWritingTo: logURL)
        h.seekToEndOfFile()
        return h
    }

    func readStatus() -> Status? {
        guard let data = try? Data(contentsOf: statusURL) else { return nil }
        return try? JSONDecoder().decode(Status.self, from: data)
    }

    enum Health { case ok, stale, failing }

    func health(_ s: Status?) -> (Health, String) {
        if let e = launchError { return (.failing, e) }
        guard let s = s else { return (.stale, "waiting for first run") }
        if !s.ok { return (.failing, s.error ?? "last poll failed") }
        if s.notify_errors > 0 { return (.failing, "notifications are being blocked") }
        if let iso = s.last_success, let d = ISO8601DateFormatter().date(from: iso),
           Date().timeIntervalSince(d) > interval * 3 + 60 {
            return (.stale, "no successful poll since \(ago(iso))")
        }
        return (.ok, "healthy")
    }

    func refresh() {
        let s = readStatus()
        let (h, _) = health(s)
        // Badge counts only my own PRs that are out of draft (i.e. in review).
        let pending = s?.items.filter { $0.mine == true && $0.draft != true && $0.needsYou }.count ?? 0
        if let b = item.button {
            if h == .ok, let mascot = Bundle.main.image(forResource: "menubar") {
                mascot.size = NSSize(width: 18, height: 18)  // full-colour, not a template
                mascot.isTemplate = false
                b.image = mascot
            } else {
                // Unhealthy: a monochrome warning is easier to notice than the mascot.
                b.image = NSImage(systemSymbolName: "exclamationmark.triangle", accessibilityDescription: "PRNotify problem")
                b.image?.isTemplate = true
            }
            b.title = (h == .ok && pending > 0) ? " \(pending)" : ""
        }
    }

    // Rebuild each time it opens so the "ago" text is fresh.
    func menuWillOpen(_ menu: NSMenu) {
        menu.removeAllItems()
        let s = readStatus()
        let (h, why) = health(s)

        let head: String
        switch h {
        case .ok: head = "Healthy"
        case .stale: head = "Stale: \(why)"
        case .failing: head = "Problem: \(why)"
        }
        menu.addItem(label(head))
        if process?.isRunning == true {
            menu.addItem(label("Polling now…"))
        } else {
            menu.addItem(label("Last run: \(ago(s?.last_run))" + (s.map { "  (\($0.prs) open PRs)" } ?? "")))
        }
        if let s = s, !s.ok { menu.addItem(label("Last success: \(ago(s.last_success))")) }

        if (s?.notify_errors ?? 0) > 0 {
            add("Open Notification Settings…", #selector(openNotificationSettings))
        }

        menu.addItem(.separator())
        let items = s?.items ?? []
        if items.isEmpty {
            menu.addItem(label("No open PRs"))
        }
        for (name, group) in [("My PRs", items.filter { $0.mine == true }),
                              ("Others' PRs", items.filter { $0.mine != true })] where !group.isEmpty {
            let sub = NSMenu()
            for (kind, prs) in [("Open", group.filter { $0.draft != true }),
                                ("Draft", group.filter { $0.draft == true })] where !prs.isEmpty {
                let folder = NSMenu()
                // PRs that need me first; the rest follow, unmarked.
                for p in prs.filter({ $0.needsYou }) + prs.filter({ !$0.needsYou }) {
                    let title = p.needsYou ? "● \(p.ref) — \(p.text)" : "    \(p.ref) — \(String(p.title.prefix(60)))"
                    let mi = NSMenuItem(title: title, action: #selector(openURL(_:)), keyEquivalent: "")
                    mi.target = self
                    mi.representedObject = p.url
                    mi.toolTip = p.title
                    folder.addItem(mi)
                }
                let need = prs.filter { $0.needsYou }.count
                let fi = NSMenuItem(title: "\(kind) (\(prs.count)" + (need > 0 ? ", \(need) need you)" : ")"), action: nil, keyEquivalent: "")
                fi.submenu = folder
                sub.addItem(fi)
            }
            let need = group.filter { $0.needsYou }.count
            let parent = NSMenuItem(title: "\(name) (\(group.count)" + (need > 0 ? ", \(need) need you)" : ")"), action: nil, keyEquivalent: "")
            parent.submenu = sub
            menu.addItem(parent)
        }

        menu.addItem(.separator())
        add("Poll now", #selector(pollNow), key: "r")
        add("Open log", #selector(openLog_))
        menu.addItem(.separator())
        add("Quit PRNotify", #selector(quit), key: "q")
    }

    func label(_ t: String) -> NSMenuItem {
        let mi = NSMenuItem(title: t, action: nil, keyEquivalent: "")
        mi.isEnabled = false
        return mi
    }

    func add(_ title: String, _ sel: Selector, key: String = "") {
        let mi = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        mi.target = self
        menu.addItem(mi)
    }

    @objc func openURL(_ sender: NSMenuItem) {
        if let s = sender.representedObject as? String, let u = URL(string: s) { NSWorkspace.shared.open(u) }
    }
    @objc func pollNow() { poll() }
    @objc func openLog_() { NSWorkspace.shared.open(logURL) }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func openNotificationSettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension")!)
    }
}

// MARK: - Entry point

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = NotificationDelegate()
center.delegate = delegate
var menuBar: MenuBar?  // keep alive

if CommandLine.arguments.contains("--check") {
    center.requestAuthorization(options: [.alert, .sound]) { granted, err in
        center.getNotificationSettings { s in
            print("granted=\(granted) status=\(s.authorizationStatus.rawValue) error=\(err?.localizedDescription ?? "none")")
            exit(granted ? 0 : 2)
        }
    }
} else if let title = arg("--title") {
    center.requestAuthorization(options: [.alert, .sound]) { granted, err in
        guard granted else {
            FileHandle.standardError.write(Data("notifications not permitted (\(err?.localizedDescription ?? "denied")); enable PRNotify in System Settings > Notifications\n".utf8))
            exit(2)
        }
        let c = UNMutableNotificationContent()
        c.title = title
        c.body = arg("--body") ?? ""
        c.sound = .default
        if let u = arg("--url") { c.userInfo = ["url": u] }
        // Reusing an id replaces the earlier notification for the same PR.
        let req = UNNotificationRequest(identifier: arg("--id") ?? UUID().uuidString, content: c, trigger: nil)
        center.add(req) { err in
            if let err = err {
                FileHandle.standardError.write(Data("post failed: \(err.localizedDescription)\n".utf8))
                exit(1)
            }
            exit(0)
        }
    }
} else if CommandLine.arguments.contains("--menubar") {
    // Ask up front so the permission prompt comes from the long-lived app.
    center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    menuBar = MenuBar()
    menuBar?.start()
} else {
    // Relaunched by macOS for a notification click; the delegate opens the URL.
    DispatchQueue.main.asyncAfter(deadline: .now() + 10) { exit(0) }
}

app.run()
