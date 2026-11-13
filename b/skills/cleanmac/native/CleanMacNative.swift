// CleanMac's current-user Trash bridge. No elevated service is installed.
// Foundation contract: https://developer.apple.com/documentation/foundation/filemanager/trashitem(at:resultingitemurl:)
import Darwin
import Foundation

private let helperVersion = "1.0.0"
private let protocolVersion = 1

private struct NativeFailure: Error {
    let code: String
    let message: String
    let errnoValue: Int32?
}

private final class Descriptor {
    let value: Int32
    init(_ value: Int32) { self.value = value }
    deinit { _ = Darwin.close(value) }
}

private func failure(_ code: String, _ message: String, errnoValue: Int32? = nil) -> NativeFailure {
    NativeFailure(code: code, message: message, errnoValue: errnoValue)
}

private func systemFailure(_ operation: String) -> NativeFailure {
    let value = errno
    return failure("filesystem_error", "\(operation) failed (errno \(value)).", errnoValue: value)
}

private func emit(_ value: [String: Any], to handle: FileHandle = .standardOutput) {
    // JSONSerialization escapes newlines and other controls in untrusted names.
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else {
        handle.write(Data("{\"status\":\"error\",\"error\":{\"code\":\"encoding_error\"}}\n".utf8))
        return
    }
    handle.write(data)
    handle.write(Data([10]))
}

private func components(of path: String) throws -> [String] {
    guard path.hasPrefix("/"), !path.utf8.contains(0), path.utf8.count < 4096 else {
        throw failure("invalid_path", "Paths must be absolute local filesystem paths.")
    }
    let parts = path.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
    guard parts.count > 1, parts[0].isEmpty,
          parts.dropFirst().allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else {
        throw failure("invalid_path", "Paths must be normalized without dot or empty components.")
    }
    return Array(parts.dropFirst())
}

private func metadata(_ fd: Int32) throws -> stat {
    var value = stat()
    guard fstat(fd, &value) == 0 else { throw systemFailure("fstat") }
    return value
}

private func metadata(at name: String, parent: Int32) throws -> stat {
    var value = stat()
    guard fstatat(parent, name, &value, AT_SYMLINK_NOFOLLOW) == 0 else {
        throw systemFailure("fstatat")
    }
    return value
}

private func isDirectory(_ value: stat) -> Bool { (value.st_mode & S_IFMT) == S_IFDIR }

private func requirePrivateDirectory(_ value: stat) throws {
    guard isDirectory(value), value.st_uid == getuid(), value.st_mode & 0o777 == 0o700 else {
        throw failure("unsafe_staging", "State, quarantine, and operation directories must be owned by the current user with mode 0700.")
    }
}

private func directory(_ parts: [String], privateSuffix: Int = 0) throws -> Descriptor {
    let root = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard root >= 0 else { throw systemFailure("open root") }
    var current = Descriptor(root)
    for (index, part) in parts.enumerated() {
        let next = openat(current.value, part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard next >= 0 else {
            throw failure("unsafe_staging", "An ancestor is missing, inaccessible, or a symbolic link.", errnoValue: errno)
        }
        current = Descriptor(next)
        if index >= parts.count - privateSuffix {
            try requirePrivateDirectory(metadata(current.value))
        }
    }
    return current
}

private func sameIdentity(_ first: stat, _ second: stat) -> Bool {
    first.st_dev == second.st_dev && first.st_ino == second.st_ino
}

private func sameFile(_ first: stat, _ second: stat) -> Bool {
    sameIdentity(first, second) && first.st_size == second.st_size &&
        first.st_mtimespec.tv_sec == second.st_mtimespec.tv_sec &&
        first.st_mtimespec.tv_nsec == second.st_mtimespec.tv_nsec &&
        first.st_mode == second.st_mode && first.st_nlink == second.st_nlink &&
        first.st_flags == second.st_flags && first.st_uid == second.st_uid
}

private func requireRegularCapturedFile(_ value: stat) throws {
    // SF_DATALESS is Darwin's content-placeholder flag. 0x8000 is UF_HIDDEN,
    // not an offline marker on macOS; hidden files are not inherently unsafe.
    let unavailableFlags: UInt32 = 0x40000000
    guard value.st_mode & S_IFMT == S_IFREG, value.st_nlink == 1,
          value.st_uid == getuid(), value.st_flags & unavailableFlags == 0 else {
        throw failure("unsupported_item", "Only captured regular, single-link, locally available files owned by the current user can be trashed.")
    }
}

private func writeAll(_ data: Data, to fd: Int32) throws {
    try data.withUnsafeBytes { bytes in
        var offset = 0
        while offset < bytes.count {
            guard let base = bytes.baseAddress else { return }
            let written = Darwin.write(fd, base.advanced(by: offset), bytes.count - offset)
            if written < 0 && errno == EINTR { continue }
            guard written > 0 else { throw systemFailure("write receipt") }
            offset += written
        }
    }
}

private func writeReceipt(_ record: [String: Any], name: String, parent: Int32, initial: Bool) throws {
    let temporary = ".cleanmac-receipt-\(UUID().uuidString.lowercased()).tmp"
    let fd = openat(parent, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
    guard fd >= 0 else { throw systemFailure("create receipt") }
    let opened = Descriptor(fd)
    defer { _ = unlinkat(parent, temporary, 0) }
    let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
    try writeAll(data + Data([10]), to: opened.value)
    guard fsync(opened.value) == 0 else { throw systemFailure("sync receipt") }
    if initial {
        // Creating the pending receipt must never replace prior recovery evidence.
        guard linkat(parent, temporary, parent, name, 0) == 0 else {
            throw failure("receipt_exists", "Cannot create a new receipt without replacing existing evidence.")
        }
    } else {
        let existing = try metadata(at: name, parent: parent)
        guard existing.st_mode & S_IFMT == S_IFREG, existing.st_uid == getuid(),
              existing.st_nlink == 1, existing.st_mode & 0o077 == 0 else {
            throw failure("unsafe_receipt", "The pending receipt changed unexpectedly.")
        }
        guard renameat(parent, temporary, parent, name) == 0 else {
            throw systemFailure("replace receipt")
        }
    }
    // Remove the temporary hard link before synchronizing the initial receipt.
    if initial, unlinkat(parent, temporary, 0) != 0 { throw systemFailure("finalize receipt") }
    guard fsync(parent) == 0 else { throw systemFailure("sync receipt directory") }
}

private func trash(path: String, receiptPath: String) throws -> [String: Any] {
    guard getuid() != 0, getuid() == geteuid() else {
        throw failure("privilege_not_supported", "This helper runs only as the ordinary current user.")
    }
    let sourceParts = try components(of: path)
    let receiptParts = try components(of: receiptPath)
    guard sourceParts.count >= 4, receiptParts.count == sourceParts.count,
          sourceParts.dropLast() == receiptParts.dropLast(),
          sourceParts.last != receiptParts.last,
          sourceParts[sourceParts.count - 3] == "quarantine" else {
        throw failure("unsafe_staging", "The file and receipt must be siblings in STATE_ROOT/quarantine/OPERATION_ID.")
    }
    let operation = sourceParts[sourceParts.count - 2]
    guard !operation.isEmpty, operation.count <= 128,
          operation.unicodeScalars.allSatisfy({ CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_").contains($0) }) else {
        throw failure("unsafe_staging", "The staging operation identifier is invalid.")
    }
    // The last three directories are state root, quarantine, and operation.
    let parentParts = Array(sourceParts.dropLast())
    let parent = try directory(parentParts, privateSuffix: 3)
    let parentIdentity = try metadata(parent.value)
    let filename = sourceParts[sourceParts.count - 1]
    let receiptName = receiptParts[receiptParts.count - 1]
    var existing = stat()
    guard fstatat(parent.value, receiptName, &existing, AT_SYMLINK_NOFOLLOW) != 0, errno == ENOENT else {
        throw failure("receipt_exists", "A receipt already exists; recover or reconcile that operation instead of replaying it.")
    }
    // O_EVTONLY permits metadata validation without reading or hydrating content.
    let fileFD = openat(parent.value, filename, O_EVTONLY | O_NOFOLLOW | O_CLOEXEC)
    guard fileFD >= 0 else { throw failure("unsupported_item", "Cannot open the captured file without following links.") }
    let file = Descriptor(fileFD)
    let captured = try metadata(file.value)
    try requireRegularCapturedFile(captured)
    guard sameFile(captured, try metadata(at: filename, parent: parent.value)) else {
        throw failure("changed_item", "The captured file changed before Trash.")
    }
    var record: [String: Any] = [
        "schema_version": 1, "protocol_version": protocolVersion,
        "version": helperVersion, "status": "pending", "original_path": path,
        "device": Int(captured.st_dev), "inode": UInt64(captured.st_ino),
        "created_at": ISO8601DateFormatter().string(from: Date())
    ]
    try writeReceipt(record, name: receiptName, parent: parent.value, initial: true)
    let currentParent = try directory(parentParts, privateSuffix: 3)
    guard sameIdentity(parentIdentity, try metadata(currentParent.value)),
          sameFile(captured, try metadata(at: filename, parent: parent.value)) else {
        throw failure("changed_item", "The private staging location or captured file changed before Trash.")
    }

    // The parent executor already captured this exact inode into private staging.
    // Foundation is path-based: owner-only staging and revalidation constrain its
    // input. This is not a security boundary against a hostile same-uid process.
    var destination: NSURL?
    do {
        try FileManager.default.trashItem(at: URL(fileURLWithPath: path), resultingItemURL: &destination)
    } catch {
        let nativeError = error as NSError
        let underlying = nativeError.userInfo[NSUnderlyingErrorKey] as? NSError
        let posixError = nativeError.domain == NSPOSIXErrorDomain ? nativeError : underlying
        let posixCode = posixError?.domain == NSPOSIXErrorDomain ? posixError?.code : nil
        let denied = (nativeError.domain == NSCocoaErrorDomain &&
            [NSFileReadNoPermissionError, NSFileWriteNoPermissionError].contains(nativeError.code)) ||
            posixCode == Int(EACCES) || posixCode == Int(EPERM)
        let code = denied ? "permission_denied" : "trash_failed"
        let message = denied
            ? "macOS denied the native Trash operation. Review access for the responsible host, or keep this item; no automatic elevation or permanent deletion was attempted."
            : "Foundation could not complete Trash. Preserve the pending receipt and staging item for recovery; no permanent deletion was attempted."
        record["error"] = ["code": code, "message": message,
                           "native_domain": nativeError.domain, "native_code": nativeError.code,
                           "posix_errno": posixCode as Any? ?? NSNull()]
        // Retain pending status: a Foundation failure does not prove that no
        // filesystem effect occurred. The executor reconciles the captured inode.
        try? writeReceipt(record, name: receiptName, parent: parent.value, initial: false)
        throw failure(code, message)
    }
    guard let destination, let destinationPath = destination.path else {
        throw failure("ambiguous_result", "Trash did not return a destination. Preserve this pending operation for recovery; do not replay it.")
    }
    record["trash_path"] = destinationPath
    var moved = stat()
    guard lstat(destinationPath, &moved) == 0, sameIdentity(captured, moved),
          moved.st_mode & S_IFMT == S_IFREG else {
        record["status"] = "ambiguous"
        try writeReceipt(record, name: receiptName, parent: parent.value, initial: false)
        throw failure("ambiguous_result", "Trash destination identity could not be verified. The destination is retained in the receipt for recovery.")
    }
    record["status"] = "trashed"
    record["completed_at"] = ISO8601DateFormatter().string(from: Date())
    try writeReceipt(record, name: receiptName, parent: parent.value, initial: false)
    return record
}

private func preflight(path: String) throws -> [String: Any] {
    guard getuid() != 0, getuid() == geteuid() else {
        throw failure("privilege_not_supported", "This helper runs only as the ordinary current user.")
    }
    let sourceParts = try components(of: path)
    let sourceParent = try directory(Array(sourceParts.dropLast()))
    let source = try metadata(at: sourceParts[sourceParts.count - 1], parent: sourceParent.value)
    guard source.st_mode & S_IFMT == S_IFREG || isDirectory(source) else {
        throw failure("unsupported_item", "Trash preflight requires an existing regular file or directory.")
    }
    let trashURL: URL
    do {
        // Apple documents appropriateFor as meaningful only for replacement
        // directories. Verify the volume explicitly instead of assuming that
        // this resolves an external volume's Trash. create:false is essential.
        trashURL = try FileManager.default.url(for: .trashDirectory, in: .userDomainMask,
                                              appropriateFor: URL(fileURLWithPath: path), create: false)
    } catch {
        throw failure("trash_unavailable", "The existing Trash directory could not be resolved without creating it. Keep these files and review macOS access before cleanup.")
    }
    let trashPath = trashURL.path
    let trashParts = try components(of: trashPath)
    let trashDirectory: Descriptor
    do {
        trashDirectory = try directory(trashParts)
    } catch let error as NativeFailure {
        if error.errnoValue == EACCES || error.errnoValue == EPERM {
            throw failure("permission_denied", "The native helper cannot open the existing Trash directory for verification. Review access for the responsible host or keep these files; no files were moved.")
        }
        throw failure("trash_unavailable", "The existing Trash directory is missing or cannot be safely opened without following links. No directory was created and no files were moved.")
    }
    let trashMetadata = try metadata(trashDirectory.value)
    guard source.st_dev == trashMetadata.st_dev else {
        throw failure("cross_device_unavailable", "The resolved Trash is on another volume. No files were moved; this volume is unsupported by the current executor.")
    }
    guard trashMetadata.st_uid == getuid() else {
        throw failure("unsafe_trash", "The resolved Trash directory is not owned by the current user.")
    }
    return ["status": "available", "protocol_version": protocolVersion,
            "trash_directory": trashPath, "source_device": Int(source.st_dev),
            "trash_device": Int(trashMetadata.st_dev), "created_directory": false,
            "note": "The caller must independently verify its own access before capturing any file."]
}

private func run() throws {
    let arguments = Array(CommandLine.arguments.dropFirst())
    if arguments == ["info"] {
        emit(["protocol_version": protocolVersion, "version": helperVersion,
              "capabilities": ["trash", "trash_preflight"], "platform": "macos", "privileged": false])
        return
    }
    if arguments.count == 3, arguments[0] == "preflight", arguments[1] == "--path" {
        emit(try preflight(path: arguments[2]))
        return
    }
    guard arguments.count == 5, arguments[0] == "trash",
          arguments[1] == "--path", arguments[3] == "--receipt" else {
        throw failure("invalid_arguments", "Use info, preflight --path ABSOLUTE_SOURCE, or trash --path ABSOLUTE_STAGED_FILE --receipt ABSOLUTE_RECEIPT.")
    }
    emit(try trash(path: arguments[2], receiptPath: arguments[4]))
}

do {
    try run()
} catch let error as NativeFailure {
    emit(["status": "error", "error": ["code": error.code, "message": error.message]], to: .standardError)
    exit(1)
} catch {
    emit(["status": "error", "error": ["code": "native_error", "message": "The native operation failed; preserve its receipt and staging data for recovery."]], to: .standardError)
    exit(1)
}
