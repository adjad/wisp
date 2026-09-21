import Darwin
import Foundation

private func processGroupExists(_ processGroup: pid_t) -> Bool {
    guard processGroup > 0 else { return false }
    if kill(-processGroup, 0) == 0 { return true }
    return errno == EPERM
}

private func reapLeader(_ leader: pid_t, _ reaped: inout Bool) {
    guard !reaped else { return }
    var status: Int32 = 0
    let result = waitpid(leader, &status, WNOHANG)
    if result == leader || (result < 0 && errno == ECHILD) { reaped = true }
}

@discardableResult
func cleanupProcessGroup(_ leader: pid_t, graceMilliseconds: Int = 3_000,
                         killMilliseconds: Int = 2_000) -> Bool {
    guard leader > 0 else { return true }
    var reaped = false
    _ = kill(-leader, SIGTERM)
    let grace = Date().addingTimeInterval(Double(graceMilliseconds) / 1000.0)
    while Date() < grace {
        reapLeader(leader, &reaped)
        if !processGroupExists(leader) && reaped { return true }
        usleep(50_000)
    }
    if processGroupExists(leader) { _ = kill(-leader, SIGKILL) }
    let deadline = Date().addingTimeInterval(Double(killMilliseconds) / 1000.0)
    while Date() < deadline {
        reapLeader(leader, &reaped)
        if !processGroupExists(leader) && reaped { return true }
        usleep(50_000)
    }
    reapLeader(leader, &reaped)
    return !processGroupExists(leader) && reaped
}
