import Foundation
import CryptoKit
import Darwin

struct CloudFailure: Error { let code: String }

func hash(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

func accountID() throws -> String {
    guard let token = FileManager.default.ubiquityIdentityToken else {
        throw CloudFailure(code: "icloud_account_unavailable")
    }
    let bytes = try NSKeyedArchiver.archivedData(withRootObject: token, requiringSecureCoding: false)
    return hash(bytes)
}

// Check every component without following symlinks. No file contents are opened.
func ancestors(_ path: String) throws -> [[String: Any]] {
    guard path.hasPrefix("/"), !path.utf8.contains(0), path.count < 16_384,
          URL(fileURLWithPath: path).standardizedFileURL.path == path else {
        throw CloudFailure(code: "cloud_invalid_scope")
    }
    var current = ""
    var result: [[String: Any]] = []
    for component in path.split(separator: "/") {
        current += "/" + component
        var value = stat()
        guard lstat(current, &value) == 0, value.st_mode & S_IFMT != S_IFLNK else {
            throw CloudFailure(code: "cloud_path_unavailable")
        }
        result.append(["path": current, "device": value.st_dev, "inode": value.st_ino,
                       "kind": value.st_mode & S_IFMT])
    }
    return result
}

func metadata(_ path: String, account: String) throws -> [String: Any] {
    let chain = try ancestors(path)
    var info = stat()
    guard lstat(path, &info) == 0 else { throw CloudFailure(code: "cloud_path_unavailable") }
    let url = URL(fileURLWithPath: path)
    let resource = try url.resourceValues(forKeys: [.isUbiquitousItemKey, .ubiquitousItemIsUploadedKey,
        .ubiquitousItemIsUploadingKey, .ubiquitousItemHasUnresolvedConflictsKey,
        .ubiquitousItemDownloadingStatusKey, .isRegularFileKey, .isSymbolicLinkKey, .isPackageKey])
    let eligible = resource.isUbiquitousItem == true && resource.ubiquitousItemIsUploaded == true
        && resource.ubiquitousItemIsUploading == false && resource.ubiquitousItemHasUnresolvedConflicts == false
        && resource.ubiquitousItemDownloadingStatus == .current && resource.isRegularFile == true
        && resource.isSymbolicLink != true && resource.isPackage != true && info.st_nlink == 1
    return ["id": "icloud-" + String(hash(Data((account + path + String(info.st_ino)).utf8)).prefix(24)),
            "path": path, "ancestors": chain, "device": info.st_dev, "inode": info.st_ino,
            "size": info.st_size, "modified_seconds": info.st_mtimespec.tv_sec,
            "modified_nanoseconds": info.st_mtimespec.tv_nsec, "changed_seconds": info.st_ctimespec.tv_sec,
            "changed_nanoseconds": info.st_ctimespec.tv_nsec, "eligible": eligible,
            "ubiquitous": resource.isUbiquitousItem as Any? ?? NSNull(),
            "uploaded": resource.ubiquitousItemIsUploaded as Any? ?? NSNull(),
            "uploading": resource.ubiquitousItemIsUploading as Any? ?? NSNull(),
            "conflicts": resource.ubiquitousItemHasUnresolvedConflicts as Any? ?? NSNull(),
            "download_status": resource.ubiquitousItemDownloadingStatus?.rawValue as Any? ?? NSNull()]
}

func checked(_ request: [String: Any]) throws -> (String, [String: Any]) {
    let currentAccount = try accountID()
    guard let root = request["root"] as? String, let item = request["item"] as? [String: Any],
          let path = item["path"] as? String, request["account"] as? String == currentAccount,
          URL(fileURLWithPath: path).deletingLastPathComponent().path == root else {
        throw CloudFailure(code: "cloud_scope_changed")
    }
    let actual = try metadata(path, account: currentAccount)
    guard actual["eligible"] as? Bool == true, NSDictionary(dictionary: actual).isEqual(to: item) else {
        throw CloudFailure(code: "cloud_item_changed")
    }
    return (path, actual)
}

func run(_ command: String, _ request: [String: Any]) throws -> [String: Any] {
    if command == "info" {
        return ["protocol_version": 1, "version": "1.0.0", "capabilities": ["icloud-metadata", "local-evict"], "privileged": false]
    }
    if command == "inspect" {
        guard let root = request["root"] as? String else { throw CloudFailure(code: "cloud_invalid_scope") }
        _ = try ancestors(root)
        let account = try accountID()
        let rootURL = URL(fileURLWithPath: root, isDirectory: true)
        let values = try rootURL.resourceValues(forKeys: [.isDirectoryKey, .isUbiquitousItemKey])
        guard values.isDirectory == true, values.isUbiquitousItem == true else {
            throw CloudFailure(code: "icloud_root_unavailable")
        }
        // Enumerate direct children without opening or downloading their contents.
        var items: [[String: Any]] = []
        var errors: [[String: String]] = []
        guard let enumerator = FileManager.default.enumerator(at: rootURL, includingPropertiesForKeys: nil,
                options: [.skipsSubdirectoryDescendants, .skipsPackageDescendants], errorHandler: { url, _ in
                    errors.append(["path": url.path, "code": "cloud_metadata_unavailable"])
                    return errors.count < 1000
                }) else {
            throw CloudFailure(code: "cloud_path_unavailable")
        }
        var limited = false
        while let entry = enumerator.nextObject() as? URL {
            if items.count + errors.count >= 1000 { limited = true; break }
            do { items.append(try metadata(entry.path, account: account)) }
            catch { errors.append(["path": entry.path, "code": "cloud_metadata_unavailable"]) }
        }
        return ["status": limited || !errors.isEmpty ? "partial" : "complete", "root": root,
                "account": account, "items": items, "errors": errors, "limit_reached": limited,
                "scope_depth": "direct-children", "contents_read": false]
    }
    if command == "validate" || command == "evict" {
        let (path, item) = try checked(request)
        if command == "validate" { return ["status": "complete", "eligible": true] }
        // Apple explicitly says not to wrap this API in a coordinated write.
        // The API coordinates internally and preserves the remote iCloud item.
        try FileManager.default.evictUbiquitousItem(at: URL(fileURLWithPath: path))
        return ["status": "completed", "action": "local-evict", "path": path,
                "remote_preserved": true, "provider_api_accepted": true,
                "verification": "Foundation accepted eviction; asynchronous local space accounting is not a guaranteed reclaimed-byte measurement.",
                "recovery": "Choose Download Now in Finder to download the remote file again.", "item": item]
    }
    throw CloudFailure(code: "cloud_invalid_command")
}

do {
    let command = CommandLine.arguments.count == 2 ? CommandLine.arguments[1] : ""
    let data = command == "info" ? Data("{}".utf8) : FileHandle.standardInput.readData(ofLength: 65_537)
    guard data.count <= 65_536, let request = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        throw CloudFailure(code: "cloud_invalid_request")
    }
    let result = try run(command, request)
    let encoded = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(encoded + Data("\n".utf8))
} catch {
    let code = (error as? CloudFailure)?.code ?? "cloud_native_failed"
    let result: [String: Any] = ["error": ["code": code]]
    if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(data + Data("\n".utf8))
    }
    exit(1)
}
