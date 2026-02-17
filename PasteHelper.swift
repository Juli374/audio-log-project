import ApplicationServices
import CoreGraphics
import Foundation

let logFile: FileHandle? = {
    let path = NSHomeDirectory() + "/Library/Logs/audio-log/paste-helper.log"
    if !FileManager.default.fileExists(atPath: path) {
        FileManager.default.createFile(atPath: path, contents: nil)
    }
    let fh = FileHandle(forWritingAtPath: path)
    fh?.seekToEndOfFile()
    return fh
}()

func log(_ msg: String) {
    let line = "\(Date()): \(msg)\n"
    logFile?.seekToEndOfFile()
    logFile?.write(Data(line.utf8))
    FileHandle.standardError.write(Data("PasteHelper: \(msg)\n".utf8))
}

// Small delay for app initialization when launched via 'open'
usleep(100_000)

let text: String? = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : nil
log("started, args=\(CommandLine.arguments.count), text=\(text?.prefix(40) ?? "nil")")

// Check accessibility
let trusted = AXIsProcessTrusted()
log("AXIsProcessTrusted: \(trusted)")

// --- Try Accessibility API (direct text insertion) ---
if let text = text, !text.isEmpty {
    let systemWide = AXUIElementCreateSystemWide()
    var focusedRef: AnyObject?
    let err = AXUIElementCopyAttributeValue(
        systemWide,
        kAXFocusedUIElementAttribute as CFString,
        &focusedRef
    )
    log("AXFocusedUIElement: err=\(err.rawValue) (0=ok)")

    if err == .success, let element = focusedRef {
        let axElement = element as! AXUIElement
        var roleRef: AnyObject?
        AXUIElementCopyAttributeValue(axElement, kAXRoleAttribute as CFString, &roleRef)
        log("focused role: \(roleRef ?? "unknown" as AnyObject)")

        let setErr = AXUIElementSetAttributeValue(
            axElement,
            kAXSelectedTextAttribute as CFString,
            text as CFTypeRef
        )
        log("AXSetSelectedText: err=\(setErr.rawValue) (0=ok)")
        if setErr == .success {
            log("SUCCESS via AXSelectedText")
            exit(0)
        }
    }
    log("AX failed, falling back to CGEvent Cmd+V")
}

// --- Fallback: CGEvent Cmd+V ---
let source = CGEventSource(stateID: .hidSystemState)
log("CGEventSource: \(source != nil ? "ok" : "nil")")
let keyV: CGKeyCode = 9

if let down = CGEvent(keyboardEventSource: source, virtualKey: keyV, keyDown: true) {
    down.flags = .maskCommand
    down.post(tap: .cghidEventTap)
    log("CGEvent keyDown posted")
} else {
    log("CGEvent keyDown FAILED to create")
}

usleep(50_000)

if let up = CGEvent(keyboardEventSource: source, virtualKey: keyV, keyDown: false) {
    up.flags = .maskCommand
    up.post(tap: .cghidEventTap)
    log("CGEvent keyUp posted")
} else {
    log("CGEvent keyUp FAILED to create")
}

usleep(50_000)
log("done (CGEvent fallback)")
exit(0)
