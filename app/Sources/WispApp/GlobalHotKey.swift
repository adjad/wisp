import AppKit
import Carbon.HIToolbox

// System-wide hotkey via Carbon (works without Accessibility permission).
final class GlobalHotKey {
    private var ref: EventHotKeyRef?
    private static var handlerInstalled = false
    private static var callbacks: [UInt32: () -> Void] = [:]
    private static var nextID: UInt32 = 1

    init(keyCode: UInt32, modifiers: UInt32, action: @escaping () -> Void) {
        let id = GlobalHotKey.nextID
        GlobalHotKey.nextID += 1
        GlobalHotKey.callbacks[id] = action
        GlobalHotKey.installHandlerIfNeeded()

        let hotKeyID = EventHotKeyID(signature: OSType(0x57495350), id: id) // 'WISP'
        RegisterEventHotKey(keyCode, modifiers, hotKeyID,
                            GetApplicationEventTarget(), 0, &ref)
    }

    private static func installHandlerIfNeeded() {
        guard !handlerInstalled else { return }
        handlerInstalled = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            var hkID = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject),
                              EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hkID)
            GlobalHotKey.callbacks[hkID.id]?()
            return noErr
        }, 1, &spec, nil, nil)
    }
}
