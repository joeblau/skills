// Optional user-level native APIs. No private Photos or revision-store access.
import AppKit
import ApplicationServices
import Carbon
import CryptoKit
import Darwin
import Foundation
import ImageIO
import Photos
import Security
import Vision

struct FeatureError: Error {
    let code: String
    let message: String
}

func fail(_ code: String, _ message: String) -> FeatureError {
    FeatureError(code: code, message: message)
}

func jsonData(_ object: Any) throws -> Data {
    try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
}

func sha256(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

func trueFlag(_ value: Any?) -> Bool {
    guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return false }
    return number.boolValue
}

func boundedInteger(_ request: [String: Any], _ key: String, _ defaultValue: Int, _ range: ClosedRange<Int>) throws -> Int {
    guard let value = request[key] else { return defaultValue }
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue == Double(number.intValue), range.contains(number.intValue) else {
        throw fail("invalid_arguments", "\(key) is outside the supported range.")
    }
    return number.intValue
}

func strings(_ value: Any?, limit: Int = 500) throws -> [String] {
    guard let values = value as? [String], values.count <= limit,
          values.allSatisfy({ !$0.isEmpty && !$0.contains("\0") && $0.utf8.count <= 4096 }),
          Set(values).count == values.count else {
        throw fail("invalid_arguments", "Provide a bounded list of distinct identifiers.")
    }
    return values
}

func statSnapshot(_ value: stat) -> [String: Any] {
    ["device": UInt64(value.st_dev), "inode": value.st_ino, "size": value.st_size,
     "mtime_sec": value.st_mtimespec.tv_sec, "mtime_nsec": value.st_mtimespec.tv_nsec,
     "ctime_sec": value.st_ctimespec.tv_sec, "ctime_nsec": value.st_ctimespec.tv_nsec]
}

func safeRead(_ path: String, limit: Int = 64 * 1024 * 1024) throws -> (Data, [String: Any]) {
    let components = path.split(separator: "/").map(String.init)
    guard path.hasPrefix("/"), !path.contains("\0"),
          path == "/" + components.joined(separator: "/"), !components.contains("."), !components.contains("..") else {
        throw fail("unsafe_path", "Choose an absolute normalized local file path.")
    }
    guard let leaf = components.last, !leaf.lowercased().hasSuffix(".icloud"),
          !components.contains(where: { $0.lowercased().hasSuffix(".photoslibrary") || $0 == ".DocumentRevisions-V100" }) else {
        throw fail("protected_item", "Library internals and cloud placeholders are not file inputs.")
    }
    var parent = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard parent >= 0 else { throw fail("permission_denied", "Cannot open the filesystem root.") }
    defer { close(parent) }
    for component in components.dropLast() {
        let child = openat(parent, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard child >= 0 else { throw fail("unsafe_path", "An ancestor is inaccessible or symbolic.") }
        close(parent)
        parent = child
    }
    let descriptor = openat(parent, leaf, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
    guard descriptor >= 0 else { throw fail("permission_denied", "The selected file cannot be opened.") }
    defer { close(descriptor) }
    var before = stat()
    guard fstat(descriptor, &before) == 0, (before.st_mode & S_IFMT) == S_IFREG,
          before.st_size >= 0, before.st_size <= limit,
          (before.st_flags & UInt32(SF_DATALESS)) == 0 else {
        throw fail("unsupported_file", "Only local regular files up to 64 MiB can be read; cloud placeholders are skipped.")
    }
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 65536)
    while true {
        let count = read(descriptor, &buffer, buffer.count)
        guard count >= 0 else { throw fail("read_failed", "Reading the selected file failed.") }
        if count == 0 { break }
        data.append(contentsOf: buffer.prefix(count))
        guard data.count <= limit else { throw fail("file_changed", "The selected file grew during reading.") }
    }
    var after = stat()
    guard fstat(descriptor, &after) == 0,
          NSDictionary(dictionary: statSnapshot(before)).isEqual(to: statSnapshot(after)) else {
        throw fail("file_changed", "The selected file changed while being read.")
    }
    return (data, statSnapshot(after))
}

func versionID(_ version: NSFileVersion) throws -> String {
    // Only serialize a value returned by Foundation. Never unarchive caller data.
    let archived = try NSKeyedArchiver.archivedData(withRootObject: version.persistentIdentifier, requiringSecureCoding: false)
    return sha256(archived)
}

func versionRecords(_ url: URL, pinned: Set<String>) throws -> ([[String: Any]], [String: NSFileVersion]) {
    let current = NSFileVersion.currentVersionOfItem(at: url)
    let otherVersions = NSFileVersion.otherVersionsOfItem(at: url) ?? []
    let versions = (current.map { [$0] } ?? []) + otherVersions
    let earliest = otherVersions.compactMap(\.modificationDate).min()
    let currentID = try current.map(versionID)
    var records: [[String: Any]] = []
    var lookup: [String: NSFileVersion] = [:]
    for version in versions {
        let identifier = try versionID(version)
        if lookup[identifier] != nil { continue }
        var reasons: [String] = []
        if identifier == currentID { reasons.append("current") }
        if version.modificationDate == earliest { reasons.append("earliest") }
        if version.isConflict { reasons.append("conflict") }
        if pinned.contains(identifier) { reasons.append("pinned") }
        let record: [String: Any] = [
            "id": identifier, "name": version.localizedName ?? "Document version",
            "modified_at": version.modificationDate?.timeIntervalSince1970 ?? NSNull(),
            "protected": !reasons.isEmpty, "protection_reasons": reasons,
            "is_conflict": version.isConflict, "is_current": identifier == currentID,
            "discardable": version.isDiscardable,
        ]
        records.append(record)
        lookup[identifier] = version
    }
    records.sort { ($0["id"] as? String ?? "") < ($1["id"] as? String ?? "") }
    return (records, lookup)
}

func documentVersions(_ request: [String: Any], remove: Bool) throws -> [String: Any] {
    guard let path = request["path"] as? String else { throw fail("invalid_arguments", "Choose one document path.") }
    let pinned = Set(try strings(request["pinned"] ?? []))
    let requested = remove ? try strings(request["selected"]) : []
    if remove && (requested.isEmpty || !trueFlag(request["confirm_history_loss"])) {
        throw fail("confirmation_required", "Selected revision removal permanently loses document history.")
    }
    let url = URL(fileURLWithPath: path)
    // Open before coordination to reject symlink ancestry and cloud placeholders.
    _ = try safeRead(path)
    var result: [String: Any] = [:]
    var actionError: Error?
    var coordinationError: NSError?
    let coordinator = NSFileCoordinator(filePresenter: nil)
    let action: (URL) -> Void = { coordinatedURL in
        do {
            guard coordinatedURL.path == url.path else { throw fail("scope_changed", "Coordination changed the selected document.") }
            let (bytes, identity) = try safeRead(path)
            let contentHash = sha256(bytes)
            let (records, lookup) = try versionRecords(coordinatedURL, pinned: pinned)
            if remove {
                guard request["document_sha256"] as? String == contentHash,
                      let expectedIdentity = request["document_identity"] as? [String: Any],
                      NSDictionary(dictionary: identity).isEqual(to: expectedIdentity),
                      let expected = request["versions"] as? [[String: Any]] else {
                    throw fail("stale_plan", "The reviewed document identity, bytes, or version plan changed.")
                }
                let expectedIDs = try strings(expected.compactMap { $0["id"] as? String })
                guard expectedIDs.count == expected.count else { throw fail("invalid_selection", "Every reviewed revision needs a distinct identifier.") }
                let expectedByID = Dictionary(uniqueKeysWithValues: zip(expectedIDs, expected))
                let currentByID = Dictionary(uniqueKeysWithValues: records.compactMap { row in (row["id"] as? String).map { ($0, row) } })
                for identifier in requested {
                    guard let prior = expectedByID[identifier], prior["protected"] as? Bool == false else { throw fail("protected_version", "Only reviewed, unprotected revisions may be removed.") }
                    if let row = currentByID[identifier] {
                        guard row["protected"] as? Bool == false, NSDictionary(dictionary: row).isEqual(to: prior) else {
                            throw fail("stale_plan", "A selected version changed or became protected.")
                        }
                    }
                }
                var removed: [String] = []
                var missing: [String] = []
                for identifier in requested {
                    guard let version = lookup[identifier] else { missing.append(identifier); continue }
                    try version.remove()
                    removed.append(identifier)
                }
                let (after, _) = try safeRead(path)
                guard sha256(after) == contentHash else { throw fail("verification_failed", "Document contents changed during revision removal; inspect the document.") }
                let (remaining, _) = try versionRecords(coordinatedURL, pinned: pinned)
                let remainingIDs = Set(remaining.compactMap { $0["id"] as? String })
                let protectedIDs = Set(records.filter { $0["protected"] as? Bool == true }.compactMap { $0["id"] as? String })
                guard Set(removed).isDisjoint(with: remainingIDs), protectedIDs.isSubset(of: remainingIDs) else {
                    throw fail("verification_failed", "Version removal could not be verified against the protected history.")
                }
                result = ["status": "complete", "removed": removed, "skipped_missing": missing, "current_document_unchanged": true,
                          "recovery": "Removed document revisions cannot be restored by CleanMac."]
            } else {
                result = ["status": "complete", "path": path, "versions": records, "document_sha256": contentHash,
                          "document_identity": identity, "pinned": pinned.sorted(), "coverage": "local-versions-only",
                          "nonlocal_versions": "not-fetched", "action": "Individual NSFileVersion removal permanently loses selected history."]
            }
        } catch { actionError = error }
    }
    if remove { coordinator.coordinate(writingItemAt: url, options: .forMerging, error: &coordinationError, byAccessor: action) }
    else { coordinator.coordinate(readingItemAt: url, options: .withoutChanges, error: &coordinationError, byAccessor: action) }
    if let error = coordinationError { throw fail("coordination_failed", error.localizedDescription) }
    if let error = actionError { throw error }
    return result
}

func thumbnail(_ bytes: Data) throws -> (CGImage, [String: Any]) {
    guard let source = CGImageSourceCreateWithData(bytes as CFData, [kCGImageSourceShouldCache: false] as CFDictionary),
          CGImageSourceGetCount(source) > 0,
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any],
          let width = properties[kCGImagePropertyPixelWidth as String] as? Int,
          let height = properties[kCGImagePropertyPixelHeight as String] as? Int,
          width > 0, height > 0, width <= 100000, height <= 100000,
          Int64(width) * Int64(height) <= 300_000_000 else {
        throw fail("unsupported_image", "Image properties are malformed or exceed 300 megapixels / 100,000 pixels on either dimension.")
    }
    let options: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true, kCGImageSourceThumbnailMaxPixelSize: 512,
        kCGImageSourceShouldCacheImmediately: true]
    guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
        throw fail("unsupported_image", "ImageIO could not decode a bounded thumbnail.")
    }
    let alpha = properties[kCGImagePropertyHasAlpha as String] as? Bool ?? false
    let exif = properties[kCGImagePropertyExifDictionary as String] as? [String: Any] ?? [:]
    return (image, ["width": width, "height": height, "has_alpha": alpha,
                    "capture_date": exif[kCGImagePropertyExifDateTimeOriginal as String] ?? NSNull(),
                    "orientation_normalized": true, "thumbnail_max_pixels": 512,
                    "image_count": CGImageSourceGetCount(source)])
}

func featurePrint(_ image: CGImage) throws -> VNFeaturePrintObservation {
    let request = VNGenerateImageFeaturePrintRequest()
    request.revision = VNGenerateImageFeaturePrintRequestRevision1
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    guard let value = request.results?.first else { throw fail("image_analysis_failed", "Vision returned no image feature print.") }
    return value
}

func perceptualHash(_ image: CGImage) throws -> UInt64 {
    var pixels = [UInt8](repeating: 0, count: 64)
    let rendered = pixels.withUnsafeMutableBytes { memory -> Bool in
        guard let context = CGContext(data: memory.baseAddress, width: 8, height: 8, bitsPerComponent: 8,
                bytesPerRow: 8, space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return false }
        context.interpolationQuality = .high
        context.draw(image, in: CGRect(x: 0, y: 0, width: 8, height: 8))
        return true
    }
    guard rendered else { throw fail("image_analysis_failed", "A bounded perceptual hash could not be generated.") }
    let average = pixels.reduce(0) { $0 + Int($1) } / 64
    return pixels.enumerated().reduce(UInt64(0)) { result, value in result | (Int(value.element) >= average ? UInt64(1) << value.offset : 0) }
}

func candidates(_ values: [(String, VNFeaturePrintObservation, Bool, UInt64)], threshold: Float) throws -> [[String: Any]] {
    var matches: [[String: Any]] = []
    for index in values.indices {
        for other in values.indices where other > index {
            // Preserve alpha distinctions; similarity never implies equivalence.
            if values[index].2 != values[other].2 { continue }
            let hashDistance = (values[index].3 ^ values[other].3).nonzeroBitCount
            if hashDistance > 24 { continue }
            var distance: Float = 0
            try values[index].1.computeDistance(&distance, to: values[other].1)
            if distance.isFinite && distance <= threshold {
                matches.append(["item_ids": [values[index].0, values[other].0], "distance": distance,
                                "hash_distance": hashDistance, "selected": false, "requires_visual_review": true])
            }
        }
    }
    matches.sort { ($0["distance"] as? Float ?? 0) < ($1["distance"] as? Float ?? 0) }
    return matches
}

func similarImages(_ request: [String: Any]) throws -> [String: Any] {
    let paths = try strings(request["paths"], limit: 200)
    let threshold = Float(try boundedInteger(request, "threshold", 8, 1...40))
    var items: [[String: Any]] = []
    var prints: [(String, VNFeaturePrintObservation, Bool, UInt64)] = []
    var errors: [[String: Any]] = []
    for path in paths {
        do {
            let (bytes, identity) = try safeRead(path)
            let (image, metadata) = try thumbnail(bytes)
            let identifier = sha256(Data(path.utf8))
            var item = metadata
            item.merge(["id": identifier, "path": path, "content_sha256": sha256(bytes), "identity": identity,
                        "selected": false, "preview_path": path]) { _, new in new }
            let perceptual = try perceptualHash(image)
            item["perceptual_hash"] = String(format: "%016llx", perceptual)
            prints.append((identifier, try featurePrint(image), metadata["has_alpha"] as? Bool ?? false, perceptual))
            items.append(item)
        } catch {
            errors.append(["path": path, "reason": (error as? FeatureError)?.code ?? "image_analysis_failed"])
        }
    }
    return ["status": errors.isEmpty ? "complete" : "partial", "items": items,
            "pairs": try candidates(prints, threshold: threshold), "errors": errors,
            "algorithm": "vision-revision-1-ahash64-imageio-512-v1", "threshold": threshold,
            "selection_policy": "human-visual-review-required", "raw_and_live_photo_policy": "First rendered image only; paired assets are not automatically removed."]
}

func authorizationName(_ value: PHAuthorizationStatus) -> String {
    switch value {
    case .notDetermined: return "not-determined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .authorized: return "authorized"
    case .limited: return "limited"
    @unknown default: return "unknown"
    }
}

func photoRecord(_ asset: PHAsset) -> [String: Any] {
    let resources = PHAssetResource.assetResources(for: asset).map {
        ["type": $0.type.rawValue, "uti": $0.uniformTypeIdentifier, "filename": $0.originalFilename] as [String: Any]
    }
    let protected = asset.isFavorite || asset.isHidden || asset.sourceType != .typeUserLibrary
    return ["id": asset.localIdentifier, "width": asset.pixelWidth, "height": asset.pixelHeight,
            "media_type": asset.mediaType.rawValue, "media_subtypes": asset.mediaSubtypes.rawValue,
            "created_at": asset.creationDate?.timeIntervalSince1970 ?? NSNull(),
            "modified_at": asset.modificationDate?.timeIntervalSince1970 ?? NSNull(),
            "favorite": asset.isFavorite, "hidden": asset.isHidden, "source_type": asset.sourceType.rawValue,
            "burst_id": asset.burstIdentifier ?? NSNull(), "resources": resources,
            "protected": protected, "local_availability": "unknown", "selected": false]
}

func photosInspect(_ request: [String: Any]) throws -> [String: Any] {
    let status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    guard status == .authorized || status == .limited else {
        return ["status": "permission-needed", "authorization": authorizationName(status), "coverage": "unscanned",
                "items": [], "request_permission_explicitly": true]
    }
    let limit = try boundedInteger(request, "limit", 200, 1...1000)
    let offset = try boundedInteger(request, "offset", 0, 0...100000)
    let identifiers = try strings(request["selected"] ?? [], limit: 1000)
    if !identifiers.isEmpty && offset != 0 { throw fail("invalid_arguments", "Use offset zero when inspecting exact asset IDs.") }
    let options = PHFetchOptions()
    options.fetchLimit = offset + limit + 1
    options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
    options.includeHiddenAssets = false
    options.includeAllBurstAssets = true
    let fetch = identifiers.isEmpty ? PHAsset.fetchAssets(with: options) : PHAsset.fetchAssets(withLocalIdentifiers: identifiers, options: options)
    var items: [[String: Any]] = []
    let start = min(offset, fetch.count)
    let end = min(offset + limit, fetch.count)
    for index in start..<end { items.append(photoRecord(fetch.object(at: index))) }
    var result: [String: Any] = ["status": status == .limited || offset > 0 || fetch.count > end ? "partial" : "complete",
            "authorization": authorizationName(status), "coverage": status == .limited ? "limited-library" : "authorized-library",
            "items": items, "limit": limit, "offset": offset, "has_more": fetch.count > end,
            "next_offset": fetch.count > end ? end as Any : NSNull(), "pagination": "Creation-date order; library changes may shift pages. Saved selections remain exact asset IDs.",
            "network_downloads": false, "selection_policy": "explicit-assets-only",
            "deletion_effect": "Deletes entire selected assets, including Live Photo/RAW components; syncs through iCloud Photos. Recover in Photos Recently Deleted while available."]
    if trueFlag(request["include_similar"]) {
        var prints: [(String, VNFeaturePrintObservation, Bool, UInt64)] = []
        var unavailable: [String] = []
        let imageOptions = PHImageRequestOptions()
        imageOptions.isSynchronous = true
        imageOptions.isNetworkAccessAllowed = false
        imageOptions.deliveryMode = .highQualityFormat
        imageOptions.resizeMode = .exact
        for index in start..<min(end, start + 100) {
            let asset = fetch.object(at: index)
            guard asset.mediaType == .image else { continue }
            var image: CGImage?
            PHImageManager.default().requestImage(for: asset, targetSize: CGSize(width: 512, height: 512),
                contentMode: .aspectFit, options: imageOptions) { nativeImage, _ in
                    image = nativeImage?.cgImage(forProposedRect: nil, context: nil, hints: nil)
                }
            guard let image else { unavailable.append(asset.localIdentifier); continue }
            let alpha = image.alphaInfo != .none && image.alphaInfo != .noneSkipFirst && image.alphaInfo != .noneSkipLast
            do { prints.append((asset.localIdentifier, try featurePrint(image), alpha, try perceptualHash(image))) }
            catch { unavailable.append(asset.localIdentifier) }
        }
        result["similarity"] = ["algorithm": "vision-revision-1-ahash64-imageio-512-v1",
            "pairs": try candidates(prints, threshold: 8), "unavailable_thumbnail_ids": unavailable,
            "thumbnail_limit": 100, "network_downloads": false, "requires_visual_review": true,
            "coverage": end - start > 100 || !unavailable.isEmpty ? "partial" : "complete"]
    }
    return result
}

func photosDelete(_ request: [String: Any]) throws -> [String: Any] {
    guard trueFlag(request["confirm_synced_deletion"]),
          let expected = request["assets"] as? [[String: Any]], !expected.isEmpty, expected.count <= 1000 else {
        throw fail("confirmation_required", "Review selected assets and iCloud Photos deletion effects first.")
    }
    let identifiers = try strings(expected.compactMap { $0["id"] as? String }, limit: 1000)
    guard identifiers.count == expected.count else { throw fail("invalid_selection", "Every selected asset needs an identifier.") }
    let status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    guard status == .authorized || status == .limited else { throw fail("permission_denied", "Photo library access is unavailable.") }
    let fetch = PHAsset.fetchAssets(withLocalIdentifiers: identifiers, options: nil)
    var assets: [PHAsset] = []
    fetch.enumerateObjects { asset, _, _ in assets.append(asset) }
    guard assets.count == identifiers.count else { throw fail("stale_plan", "A selected asset disappeared or is no longer accessible.") }
    let expectedByID = Dictionary(uniqueKeysWithValues: zip(identifiers, expected))
    for asset in assets {
        let current = photoRecord(asset)
        guard current["protected"] as? Bool == false,
              let prior = expectedByID[asset.localIdentifier], NSDictionary(dictionary: current).isEqual(to: prior) else {
            throw fail("stale_plan", "A selected asset changed or is protected.")
        }
    }
    try PHPhotoLibrary.shared().performChangesAndWait { PHAssetChangeRequest.deleteAssets(assets as NSArray) }
    let remaining = PHAsset.fetchAssets(withLocalIdentifiers: identifiers, options: nil)
    guard remaining.count == 0 else { throw fail("verification_failed", "Some requested deletions remain visible; inspect Photos before retrying.") }
    return ["status": "complete", "deleted_asset_ids": identifiers,
            "recovery": "Use Photos Recently Deleted; recovery availability and iCloud synchronization are controlled by Photos."]
}

func processInfo(_ pid: pid_t) throws -> proc_bsdinfo {
    var value = proc_bsdinfo()
    errno = 0
    guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &value, Int32(MemoryLayout<proc_bsdinfo>.stride)) == MemoryLayout<proc_bsdinfo>.stride else {
        if errno == ESRCH { throw fail("process_exited", "The selected process no longer exists.") }
        throw fail("process_unavailable", "The selected process cannot be inspected; termination is not established.")
    }
    return value
}

func canonicalSystemPath(_ path: String) -> String {
    // Foundation may shorten /private/var to /var. Expand only macOS's known,
    // root-owned aliases; never resolve an arbitrary selected symbolic link.
    for (source, destination) in [("/var", "/private/var"), ("/tmp", "/private/tmp"), ("/etc", "/private/etc")] {
        if path == source || path.hasPrefix(source + "/") {
            var metadata = stat()
            if lstat(source, &metadata) == 0, metadata.st_uid == 0, (metadata.st_mode & S_IFMT) == S_IFLNK,
               let actual = try? FileManager.default.destinationOfSymbolicLink(atPath: source),
               actual == destination || actual == String(destination.dropFirst()) {
                return destination + path.dropFirst(source.count)
            }
        }
    }
    return path
}

struct ExecutableIdentity {
    let path: String
    let device: dev_t
    let inode: ino_t
    let uid: uid_t
    let codeHash: Data
}

func processExecutable(_ pid: pid_t) throws -> ExecutableIdentity {
    let info = try processInfo(pid)
    guard info.pbi_uid == getuid(), info.pbi_ruid == getuid(), getuid() == geteuid() else {
        throw fail("watchdog_parent_rejected", "The watchdog requires a native helper running as the same user.")
    }
    // proc_info.h defines PROC_PIDPATHINFO_MAXSIZE as 4 * MAXPATHLEN; the
    // compound macro itself is not imported by Swift.
    var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    let length = proc_pidpath(pid, &buffer, UInt32(buffer.count))
    guard length > 0, length < buffer.count else {
        throw fail("watchdog_parent_rejected", "The watchdog cannot verify the launching executable.")
    }
    let path = canonicalSystemPath(String(cString: buffer))
    var metadata = stat()
    guard lstat(path, &metadata) == 0, (metadata.st_mode & S_IFMT) == S_IFREG else {
        throw fail("watchdog_parent_rejected", "The watchdog requires a verified regular executable.")
    }
    var code: SecCode?
    let attributes = [kSecGuestAttributePid as String: NSNumber(value: pid)] as CFDictionary
    guard SecCodeCopyGuestWithAttributes(nil, attributes, [], &code) == errSecSuccess, let code,
          SecCodeCheckValidity(code, [], nil) == errSecSuccess else {
        throw fail("watchdog_parent_rejected", "The watchdog cannot verify the running code identity.")
    }
    var staticCode: SecStaticCode?
    guard SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let staticCode else {
        throw fail("watchdog_parent_rejected", "The watchdog cannot obtain its validated executable signature.")
    }
    var information: CFDictionary?
    guard SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &information) == errSecSuccess,
          let values = information as? [String: Any], let hash = values[kSecCodeInfoUnique as String] as? Data else {
        throw fail("watchdog_parent_rejected", "The watchdog cannot read the running code identity.")
    }
    return ExecutableIdentity(path: path, device: metadata.st_dev, inode: metadata.st_ino, uid: info.pbi_uid, codeHash: hash)
}

struct WatchdogParent {
    let pid: pid_t
    let startSeconds: UInt64
    let startMicroseconds: UInt64
    let executable: ExecutableIdentity
}

func authorizedWatchdogParent() throws -> WatchdogParent {
    let parent = getppid()
    guard parent > 1 else { throw fail("watchdog_parent_rejected", "The watchdog has no native helper parent.") }
    do {
        let before = try processInfo(parent)
        let ownExecutable = try processExecutable(getpid())
        let parentExecutable = try processExecutable(parent)
        let after = try processInfo(parent)
        guard getppid() == parent, before.pbi_start_tvsec == after.pbi_start_tvsec,
              before.pbi_start_tvusec == after.pbi_start_tvusec,
              ownExecutable.path == parentExecutable.path, ownExecutable.device == parentExecutable.device,
              ownExecutable.inode == parentExecutable.inode, ownExecutable.uid == parentExecutable.uid,
              ownExecutable.codeHash == parentExecutable.codeHash else {
            throw fail("watchdog_parent_rejected", "The watchdog can only protect the same native helper that launched it.")
        }
        return WatchdogParent(pid: parent, startSeconds: after.pbi_start_tvsec,
                              startMicroseconds: after.pbi_start_tvusec, executable: parentExecutable)
    } catch {
        throw fail("watchdog_parent_rejected", "The watchdog can only protect its verified native helper parent; no process was signalled.")
    }
}

func watchdogParentMatches(_ expected: WatchdogParent) -> Bool {
    guard let current = try? authorizedWatchdogParent() else { return false }
    return current.pid == expected.pid && current.startSeconds == expected.startSeconds &&
        current.startMicroseconds == expected.startMicroseconds && current.executable.path == expected.executable.path &&
        current.executable.device == expected.executable.device && current.executable.inode == expected.executable.inode &&
        current.executable.uid == expected.executable.uid && current.executable.codeHash == expected.executable.codeHash
}

func verifyWatchdogChild() throws {
    let child = Process()
    child.executableURL = URL(fileURLWithPath: try processExecutable(getpid()).path)
    child.arguments = ["watchdog-validate"]
    child.standardInput = FileHandle.nullDevice
    let output = Pipe()
    child.standardOutput = output
    child.standardError = FileHandle.nullDevice
    try child.run()
    child.waitUntilExit()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    guard child.terminationStatus == 0, data.count <= 4096,
          let result = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          result["status"] as? String == "complete", trueFlag(result["eligible"]) else {
        throw fail("watchdog_unavailable", "The independent screen-cleaning watchdog could not verify its parent.")
    }
}

func ancestorPIDs() -> Set<pid_t>? {
    var result: Set<pid_t> = []
    var current = getpid()
    while current > 1 {
        guard result.insert(current).inserted, let info = try? processInfo(current), info.pbi_ppid > 0 else { return nil }
        current = pid_t(info.pbi_ppid)
    }
    return current == 1 ? result : nil
}

func safeBundlePath(_ path: String) throws {
    let components = path.split(separator: "/").map(String.init)
    guard path.hasPrefix("/"), path == "/" + components.joined(separator: "/"),
          !components.contains("."), !components.contains(".."), path.hasSuffix(".app") else {
        throw fail("unsafe_application", "A normalized application bundle path is required.")
    }
    var descriptor = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard descriptor >= 0 else { throw fail("unsafe_application", "Cannot validate application ancestry.") }
    defer { close(descriptor) }
    var currentPath = ""
    let administratorGroup = getgrnam("admin")?.pointee.gr_gid
    for component in components {
        let next = openat(descriptor, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard next >= 0 else { throw fail("unsafe_application", "Application ancestry contains a symlink or inaccessible directory.") }
        close(descriptor)
        descriptor = next
        currentPath += "/" + component
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0 else { throw fail("unsafe_application", "Application ancestry cannot be inspected.") }
        // macOS's shared Applications root is normally root:admin 0775.
        // This exception does not extend to an app or arbitrary writable parent.
        let systemApplications = currentPath == "/Applications" && metadata.st_uid == 0 &&
            administratorGroup == metadata.st_gid && (metadata.st_mode & 0o002) == 0
        guard (metadata.st_mode & 0o022) == 0 || (metadata.st_uid == 0 && (metadata.st_mode & S_ISVTX) != 0) || systemApplications else {
            throw fail("unsafe_application", "Application ancestry is writable by another user.")
        }
    }
}

func signature(_ path: String) throws -> [String: Any] {
    try safeBundlePath(path)
    var code: SecStaticCode?
    guard SecStaticCodeCreateWithPath(URL(fileURLWithPath: path) as CFURL, [], &code) == errSecSuccess, let code else {
        throw fail("unsigned_application", "The selected application has no verifiable code object.")
    }
    let flags = SecCSFlags(rawValue: kSecCSCheckAllArchitectures | kSecCSStrictValidate)
    guard SecStaticCodeCheckValidity(code, flags, nil) == errSecSuccess else {
        throw fail("unsigned_application", "Application code signing validation failed.")
    }
    var information: CFDictionary?
    guard SecCodeCopySigningInformation(code, SecCSFlags(rawValue: kSecCSSigningInformation), &information) == errSecSuccess,
          let values = information as? [String: Any], let identifier = values[kSecCodeInfoIdentifier as String] as? String,
          let hash = values[kSecCodeInfoUnique as String] as? Data else {
        throw fail("unsigned_application", "Application signing identity could not be read.")
    }
    return ["valid": true, "cdhash": hash.map { String(format: "%02x", $0) }.joined(),
            "team_id": values[kSecCodeInfoTeamIdentifier as String] as? String ?? "", "identifier": identifier]
}

func protectionReasons(_ identity: [String: Any]) -> [String] {
    var reasons: [String] = []
    let path = identity["bundle_path"] as? String ?? ""
    let executable = identity["executable_path"] as? String ?? ""
    let bundleID = (identity["bundle_id"] as? String ?? "").lowercased()
    let pid = identity["pid"] as? pid_t ?? 0
    if identity["uid"] as? uid_t != getuid() { reasons.append("not-owned-by-current-user") }
    if !executable.hasPrefix(path + "/") { reasons.append("executable-outside-bundle") }
    if let ancestors = ancestorPIDs() {
        if pid <= 1 || ancestors.contains(pid) { reasons.append("current-or-ancestor-process") }
    } else { reasons.append("unverified-ancestor-chain") }
    if identity["activation_policy"] as? Int != NSApplication.ActivationPolicy.regular.rawValue { reasons.append("background-or-helper-application") }
    if path.hasPrefix("/System/") || path.hasPrefix("/usr/") || path.hasPrefix("/Library/Apple/") { reasons.append("system-application") }
    if ["codex", "claude", "chatgpt", "kimi", "ghostty", "iterm", "terminal", "vscode", "cursor", "cleanmac"].contains(where: bundleID.contains) {
        reasons.append("agent-or-runner-application")
    }
    if ["com.apple.finder", "com.apple.dock", "com.apple.systemuiserver", "com.apple.loginwindow", "com.apple.securityagent"].contains(bundleID) {
        reasons.append("critical-system-application")
    }
    return reasons
}

@MainActor
func appRecord(_ app: NSRunningApplication) throws -> [String: Any] {
    guard let launched = app.launchDate, let bundle = app.bundleURL, let executable = app.executableURL,
          let bundleID = app.bundleIdentifier, !app.isTerminated else {
        throw fail("unsupported_process", "This process is not a live application with stable launch metadata.")
    }
    let info = try processInfo(app.processIdentifier)
    let bundlePath = canonicalSystemPath(bundle.path)
    let executablePath = canonicalSystemPath(executable.path)
    var identity: [String: Any] = ["pid": app.processIdentifier, "launch_date": launched.timeIntervalSince1970,
        "bundle_id": bundleID, "bundle_path": bundlePath, "executable_path": executablePath,
        "uid": info.pbi_uid, "process_start_sec": info.pbi_start_tvsec, "process_start_usec": info.pbi_start_tvusec,
        "activation_policy": app.activationPolicy.rawValue]
    var reasons = protectionReasons(identity)
    do { identity["code_signature"] = try signature(bundlePath) }
    catch { identity["code_signature"] = ["valid": false]; reasons.append("unverifiable-code-signature") }
    return ["id": sha256(try jsonData(identity)), "identity": identity, "protected": !reasons.isEmpty,
            "protection_reasons": reasons, "can_force_quit": reasons.isEmpty]
}

@MainActor
func applicationsInspect(_ request: [String: Any]) throws -> [String: Any] {
    var selected: [pid_t] = []
    if let values = request["selected_pids"] {
        guard let values = values as? [NSNumber], values.count <= 1000,
              values.allSatisfy({ CFGetTypeID($0) != CFBooleanGetTypeID() && $0.doubleValue == Double($0.int32Value) && $0.int32Value > 1 }) else {
            throw fail("invalid_arguments", "Choose distinct positive application process IDs.")
        }
        selected = values.map(\.int32Value)
        guard Set(selected).count == selected.count else { throw fail("invalid_arguments", "Application process IDs must be distinct.") }
    }
    let apps = selected.isEmpty ? NSWorkspace.shared.runningApplications : selected.compactMap(NSRunningApplication.init(processIdentifier:))
    var items: [[String: Any]] = []
    var errors: [[String: Any]] = []
    for app in apps.prefix(1000) {
        do { items.append(try appRecord(app)) }
        catch { errors.append(["pid": app.processIdentifier, "reason": (error as? FeatureError)?.code ?? "process_unavailable"]) }
    }
    if !selected.isEmpty {
        let returned = Set(apps.map(\.processIdentifier))
        for pid in selected where !returned.contains(pid) { errors.append(["pid": pid, "reason": "process-unavailable"]) }
    }
    return ["status": errors.isEmpty && apps.count <= 1000 ? "complete" : "partial", "items": items, "errors": errors]
}

@MainActor
func validateApplication(_ request: [String: Any]) throws -> (String, [String: Any], NSRunningApplication?) {
    guard let action = request["action"] as? String, ["quit", "force-quit", "relaunch"].contains(action),
          let expected = request["expected"] as? [String: Any], let pid = expected["pid"] as? pid_t,
          let bundleID = expected["bundle_id"] as? String, let bundlePath = expected["bundle_path"] as? String,
          let executablePath = expected["executable_path"] as? String,
          let expectedSignature = expected["code_signature"] as? [String: Any], trueFlag(expectedSignature["valid"]),
          expected["launch_date"] is NSNumber, expected["process_start_sec"] is NSNumber, expected["process_start_usec"] is NSNumber else {
        throw fail("invalid_application_identity", "A complete reviewed application identity is required.")
    }
    guard protectionReasons(expected).isEmpty else { throw fail("protected_application", "System applications and the current agent/runner cannot be changed.") }
    let freshSignature = try signature(bundlePath)
    guard NSDictionary(dictionary: freshSignature).isEqual(to: expectedSignature) else {
        throw fail("stale_application", "The original application's code identity changed.")
    }
    guard let bundle = Bundle(url: URL(fileURLWithPath: bundlePath)), bundle.bundleIdentifier == bundleID else {
        throw fail("stale_application", "The original application's bundle identifier changed.")
    }
    guard let executableURL = bundle.executableURL, canonicalSystemPath(executableURL.path) == executablePath else {
        throw fail("stale_application", "The original application's executable path changed.")
    }
    if action == "relaunch" {
        guard NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty else {
            throw fail("application_already_running", "The application already has a running instance; no additional instance was opened.")
        }
        return (action, expected, nil)
    }
    guard let application = NSRunningApplication(processIdentifier: pid),
          let current = try appRecord(application)["identity"] as? [String: Any],
          NSDictionary(dictionary: current).isEqual(to: expected) else {
        throw fail("stale_application", "The selected process exited, restarted, or changed identity.")
    }
    return (action, expected, application)
}

@MainActor
func applicationAction(_ request: [String: Any], mutate: Bool) async throws -> [String: Any] {
    if mutate && !trueFlag(request["approved"]) { throw fail("confirmation_required", "Review and approve the exact application action first.") }
    let (action, expected, application) = try validateApplication(request)
    if !mutate { return ["status": "complete", "eligible": true, "identity": expected, "action": action] }
    if action == "relaunch" {
        guard let bundlePath = expected["bundle_path"] as? String else { throw fail("invalid_application_identity", "Application bundle is missing.") }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = true
        configuration.createsNewApplicationInstance = false
        let launched = try await NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: bundlePath), configuration: configuration)
        guard let identity = try appRecord(launched)["identity"] as? [String: Any],
              let expectedSignature = expected["code_signature"] as? [String: Any],
              let actualSignature = identity["code_signature"] as? [String: Any],
              NSDictionary(dictionary: expectedSignature).isEqual(to: actualSignature),
              identity["bundle_id"] as? String == expected["bundle_id"] as? String,
              identity["bundle_path"] as? String == expected["bundle_path"] as? String else {
            throw fail("application_outcome_unknown", "The launched application could not be verified against the approved identity.")
        }
        return ["status": "completed", "action": action, "prior_identity": expected, "new_identity": identity]
    }
    guard let application else { throw fail("stale_application", "The application is no longer running.") }
    let requested = action == "force-quit" ? application.forceTerminate() : application.terminate()
    guard requested else { throw fail("termination_not_requested", "macOS did not accept the selected termination request.") }
    let deadline = ContinuousClock.now + .seconds(5)
    while ContinuousClock.now < deadline {
        do {
            let current = try processInfo(application.processIdentifier)
            // PID reuse means the reviewed process ended; never touch its replacement.
            if current.pbi_start_tvsec != (expected["process_start_sec"] as? UInt64) || current.pbi_start_tvusec != (expected["process_start_usec"] as? UInt64) {
                return ["status": "completed", "action": action, "prior_identity": expected, "terminated": true]
            }
        } catch {
            if (error as? FeatureError)?.code == "process_exited" || application.isTerminated {
                return ["status": "completed", "action": action, "prior_identity": expected, "terminated": true]
            }
            throw fail("application_outcome_unknown", "The process cannot be inspected after the request; termination is not verified.")
        }
        try await Task.sleep(for: .milliseconds(100))
    }
    throw fail("termination_pending", "The application is still running, possibly showing a save dialog. No force-quit fallback was attempted; inspect it before another action.")
}

@MainActor
final class ScreenCleaner: NSObject {
    var tap: CFMachPort?
    var windows: [NSWindow] = []
    var labels: [NSTextField] = []
    var timer: Timer?
    var watchdog: Process?
    var started = ContinuousClock.now
    var duration = 0
    var finished = false

    func finish(_ reason: String) {
        guard !finished else { return }
        finished = true
        if let tap { CGEvent.tapEnable(tap: tap, enable: false); CFMachPortInvalidate(tap) }
        timer?.invalidate()
        windows.forEach { $0.orderOut(nil) }
        if let watchdog, watchdog.isRunning { watchdog.terminate() }
        emit(["status": "complete", "reason": reason, "input_restored": true])
        NSApplication.shared.stop(nil)
        // stop() may wait for another event; no event needs to be synthesized.
        CFRunLoopStop(CFRunLoopGetMain())
    }

    @objc func interrupted(_ notification: Notification) { finish("display-or-session-changed") }

    func start(duration: Int) throws {
        guard AXIsProcessTrusted(), !IsSecureEventInputEnabled() else {
            throw fail("permission_needed", "Screen cleaning requires Accessibility access and an unlocked ordinary desktop; no mode was entered.")
        }
        // Establish that the independently launched guard can authenticate this
        // process before suppressing any input or showing an overlay.
        try verifyWatchdogChild()
        self.duration = duration
        started = .now
        defer {
            if let tap { CGEvent.tapEnable(tap: tap, enable: false); CFMachPortInvalidate(tap) }
            windows.forEach { $0.orderOut(nil) }
            if let watchdog, watchdog.isRunning { watchdog.terminate() }
            NotificationCenter.default.removeObserver(self)
            NSWorkspace.shared.notificationCenter.removeObserver(self)
        }
        let events: [CGEventType] = [.keyDown, .keyUp, .flagsChanged, .leftMouseDown, .leftMouseUp, .rightMouseDown,
            .rightMouseUp, .mouseMoved, .leftMouseDragged, .rightMouseDragged, .scrollWheel,
            .otherMouseDown, .otherMouseUp, .otherMouseDragged, .tabletPointer, .tabletProximity]
        let mask = events.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << $1.rawValue) }
        let pointer = Unmanaged.passUnretained(self).toOpaque()
        tap = CGEvent.tapCreate(tap: .cghidEventTap, place: .headInsertEventTap, options: .defaultTap,
            eventsOfInterest: mask, callback: { _, type, event, pointer in
                guard let pointer else { return Unmanaged.passUnretained(event) }
                let escape = type == .keyDown && event.getIntegerValueField(.keyboardEventKeycode) == 53
                let shouldPass: Bool = MainActor.assumeIsolated {
                    let owner = Unmanaged<ScreenCleaner>.fromOpaque(pointer).takeUnretainedValue()
                    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                        owner.finish("input-control-lost")
                        return true
                    }
                    // Inspect only the Escape keycode; input is never recorded.
                    if escape {
                        owner.finish("escape")
                    }
                    return false
                }
                return shouldPass ? Unmanaged.passUnretained(event) : nil
            }, userInfo: pointer)
        guard let tap, CGEvent.tapIsEnabled(tap: tap), let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0) else {
            throw fail("input_control_unavailable", "macOS did not establish reliable input control; no overlay was shown.")
        }
        // A separate process releases input even if this app's event loop stalls.
        let watchdog = Process()
        watchdog.executableURL = URL(fileURLWithPath: try processExecutable(getpid()).path)
        watchdog.arguments = ["watchdog", String(duration)]
        watchdog.standardInput = FileHandle.nullDevice
        watchdog.standardOutput = FileHandle.nullDevice
        watchdog.standardError = FileHandle.nullDevice
        do { try watchdog.run() } catch { CFMachPortInvalidate(tap); throw error }
        self.watchdog = watchdog
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        NSApplication.shared.setActivationPolicy(.accessory)
        for screen in NSScreen.screens {
            let window = NSWindow(contentRect: screen.frame, styleMask: [.borderless], backing: .buffered, defer: false, screen: screen)
            window.level = .screenSaver
            window.backgroundColor = .black
            window.isOpaque = true
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            let label = NSTextField(labelWithString: "")
            label.textColor = .white
            label.font = NSFont.systemFont(ofSize: 24)
            label.alignment = .center
            label.frame = NSRect(x: 0, y: screen.frame.height / 2 - 50, width: screen.frame.width, height: 100)
            window.contentView?.addSubview(label)
            labels.append(label)
            windows.append(window)
            window.orderFrontRegardless()
        }
        guard !windows.isEmpty else { finish("no-displays"); return }
        NSApplication.shared.activate(ignoringOtherApps: true)
        NotificationCenter.default.addObserver(self, selector: #selector(interrupted), name: NSApplication.didChangeScreenParametersNotification, object: nil)
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(interrupted), name: NSWorkspace.willSleepNotification, object: nil)
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(interrupted), name: NSWorkspace.sessionDidResignActiveNotification, object: nil)
        tick()
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        NSApplication.shared.run()
        if !finished { finish("session-ended") }
    }

    func tick() {
        if !AXIsProcessTrusted() || IsSecureEventInputEnabled() { finish("permission-or-security-changed"); return }
        let elapsed = started.duration(to: .now).components
        let seconds = Double(elapsed.seconds) + Double(elapsed.attoseconds) / 1e18
        let remaining = duration - Int(seconds)
        // Close just before the independent hard deadline, allowing the receipt
        // to be delivered normally without extending the input lock beyond it.
        if Double(duration) - seconds <= 0.3 { finish("timeout"); return }
        labels.forEach { $0.stringValue = "Clean your screen — \(remaining)s\nPress Escape to exit" }
    }
}

func emit(_ value: [String: Any]) {
    if let data = try? jsonData(value) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    }
}

@main
struct CleanMacFeatures {
    @MainActor static func main() async {
        do {
            let command = CommandLine.arguments.dropFirst().first ?? "info"
            if command == "watchdog" {
                guard CommandLine.arguments.count == 3, let timeout = UInt32(CommandLine.arguments[2]), (1...120).contains(timeout) else {
                    throw fail("invalid_arguments", "The watchdog duration must be from 1 through 120 seconds.")
                }
                let parent = try authorizedWatchdogParent()
                sleep(timeout)
                if watchdogParentMatches(parent) { kill(parent.pid, SIGKILL) }
                return
            }
            if command == "watchdog-validate" {
                _ = try authorizedWatchdogParent()
                emit(["status": "complete", "eligible": true])
                return
            }
            if command == "watchdog-preflight" {
                try verifyWatchdogChild()
                emit(["status": "complete", "eligible": true, "input_changed": false])
                return
            }
            if command == "info" {
                emit(["protocol_version": 1, "version": "1.0.0", "privileged": false,
                      "capabilities": ["document-versions", "similar-images", "photos", "screen-cleaning", "app-process"],
                      "image_algorithm": "vision-revision-1-ahash64-imageio-512-v1", "photos_bundle_id": "com.joeblau.cleanmac.features"])
                return
            }
            var data = Data()
            while let chunk = try FileHandle.standardInput.read(upToCount: 65536), !chunk.isEmpty {
                data.append(chunk)
                if data.count > 1_048_576 { throw fail("invalid_arguments", "The native request exceeds one MiB.") }
            }
            guard data.count <= 1_048_576, let request = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw fail("invalid_arguments", "Provide a JSON object no larger than one MiB.")
            }
            switch command {
            case "versions-inspect": emit(try documentVersions(request, remove: false))
            case "versions-remove": emit(try documentVersions(request, remove: true))
            case "images-inspect": emit(try similarImages(request))
            case "photos-status": emit(["status": "complete", "authorization": authorizationName(PHPhotoLibrary.authorizationStatus(for: .readWrite)), "request_permission_explicitly": true])
            case "photos-authorize":
                guard trueFlag(request["request_permission"]) else { throw fail("confirmation_required", "Request Photos permission only after explicit user intent.") }
                let status = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
                emit(["status": status == .authorized || status == .limited ? "complete" : "permission-needed", "authorization": authorizationName(status)])
            case "photos-inspect": emit(try photosInspect(request))
            case "photos-delete": emit(try photosDelete(request))
            case "apps-inspect": emit(try applicationsInspect(request))
            case "apps-validate": emit(try await applicationAction(request, mutate: false))
            case "apps-act": emit(try await applicationAction(request, mutate: true))
            case "screen-status": emit(["status": "complete", "accessibility": AXIsProcessTrusted(), "secure_input": IsSecureEventInputEnabled(), "maximum_seconds": 120, "permission_request_automatic": false])
            case "screen-start":
                guard trueFlag(request["approved"]) else { throw fail("confirmation_required", "Screen cleaning temporarily suppresses keyboard and pointer input.") }
                let controller = ScreenCleaner()
                try controller.start(duration: boundedInteger(request, "duration", 60, 1...120))
            default: throw fail("unsupported_command", "This native command is unavailable.")
            }
        } catch {
            let feature = error as? FeatureError
            emit(["status": "error", "error": ["code": feature?.code ?? "native_error", "message": feature?.message ?? error.localizedDescription]])
            exit(1)
        }
    }
}
