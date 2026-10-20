default:
  just --list

_get_manifest app_id:
    #!/usr/bin/env bash
    set -euxo pipefail
    if [ -f "{{app_id}}.json" ]; then
        echo "{{app_id}}.json"
    elif [ -f "{{app_id}}.yaml" ]; then
        echo "{{app_id}}.yaml"
    elif [ -f "{{app_id}}.yml" ]; then
        echo "{{app_id}}.yml"
    else
        echo "Error: No manifest file found for {{app_id}}" >&2
        exit 1
    fi

_get_build_subject:
    #!/usr/bin/env bash
    set -euxo pipefail
    commit_msg=$(git log -1 --pretty=%s)
    commit_hash=$(git rev-parse --short=12 HEAD)
    subject="$commit_msg ($commit_hash)"
    subject="${subject//[^[:ascii:]]/}"
    echo "$subject"

detect-appid $path:
    #!/usr/bin/env python3
    import glob
    import os
    import sys

    import gi
    import yaml

    gi.require_version("Json", "1.0")
    from gi.repository import Json


    def detect_appid(dirname):
        files = []
        ret = (None, None)

        for ext in ("yml", "yaml", "json"):
            files.extend(glob.glob(f"{dirname}/*.{ext}"))

        for filename in files:
            appid = None
            manifest_file = os.path.basename(filename)

            if os.path.isfile(filename):
                ext = filename.split(".")[-1]

                with open(filename) as f:
                    if ext in ("yml", "yaml"):
                        manifest = yaml.safe_load(f)
                        if manifest and isinstance(manifest, dict):
                            if "app-id" in manifest:
                                appid = manifest["app-id"]
                            elif "id" in manifest:
                                appid = manifest["id"]
                    else:
                        parser = Json.Parser()
                        if parser.load_from_file(filename):
                            root_node = parser.get_root()
                            if root_node and root_node.get_node_type() == Json.NodeType.OBJECT:
                                json_object = root_node.get_object()
                                if json_object:
                                    if json_object.has_member("id"):
                                        appid = json_object.get_string_member("id")
                                    elif json_object.has_member("app-id"):
                                        appid = json_object.get_string_member("app-id")

                if not appid:
                    print(f"Did not find any app-id from file {manifest_file}")
                    continue

                if appid:
                    if os.path.splitext(manifest_file)[0] == appid:
                        print(f"Found appid: {appid}")
                        with open("app_id", "w") as f:
                            f.write(f"{appid}\n")
                        return (manifest_file, appid)
                    else:
                        print(f"app-id {appid} and filename (without extension) {manifest_file} does not match, discarding")
        return ret


    path = os.environ.get("path")

    manifest_file, appid = detect_appid(path)
    if manifest_file is None or appid is None:
        print("Failed to detect appid")
        sys.exit(1)

checkout repo ref:
    #!/usr/bin/env bash
    set -euxo pipefail
    git config --global --add safe.directory "*"
    git init
    git remote add origin {{repo}}
    git fetch --depth 1 origin {{ref}}
    git checkout FETCH_HEAD
    git submodule update --init --recursive --depth 1

prepare-env:
    #!/usr/bin/env bash
    set -euxo pipefail
    flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak remote-add --user --if-not-exists flathub-beta https://dl.flathub.org/beta-repo/flathub-beta.flatpakrepo
    git config --global --add safe.directory "*"

validate-manifest app_id:
    #!/usr/bin/env bash
    set -euxo pipefail
    git config --global --add safe.directory "*"
    manifest=$(just -f .flathub.justfile _get_manifest {{app_id}})

    case "${REF}" in
        "refs/heads/beta")
            exceptions_repo="beta"
            ;;
        refs/pull/*)
            case "${PR_TARGET_BRANCH:-master}" in
                "beta")
                    exceptions_repo="beta"
                    ;;
                *)
                    exceptions_repo="stable"
                    ;;
            esac
            ;;
        *)
            exceptions_repo="stable"
            ;;
    esac

    lint_args=(--gha-format --exceptions --exceptions-repo "$exceptions_repo")
    [ "${PERMISSION_REVIEW_INTEGRATED:-}" = "true" ] && lint_args+=(--permission-review)

    flatpak-builder-lint "${lint_args[@]}" manifest "$manifest"

download-sources app_id:
    #!/usr/bin/env bash
    set -euxo pipefail

    manifest=$(just -f .flathub.justfile _get_manifest {{app_id}})

    max_retries=5
    sleep_seconds=15

    for (( retry_count=0; retry_count<$max_retries; retry_count++ )); do
        if flatpak-builder --allow-missing-runtimes --force-clean --sandbox --download-only builddir "$manifest"; then
            exit 0
        fi

        if [[ $retry_count -lt $((max_retries - 1)) ]]; then
            echo "Attempt $((retry_count + 1)) failed. Retrying in $((sleep_seconds * (retry_count + 1))) seconds..."
            sleep $((sleep_seconds * (retry_count + 1)))
        fi
    done

    echo "Failed after $max_retries attempts"
    exit 1

build app_id git_ref build_arch:
    #!/usr/bin/env bash
    set -euxo pipefail

    case "{{git_ref}}" in
        "refs/heads/master")
            ref_branch="stable"
            ;;
        "refs/heads/beta")
            ref_branch="beta"
            ;;
        refs/heads/branch/*)
            ref_branch_tmp="{{git_ref}}"
            ref_branch="${ref_branch_tmp##refs/heads/branch/}"
            ;;
        *)
            ref_branch="test"
            ;;
    esac

    manifest=$(just -f .flathub.justfile _get_manifest {{app_id}})
    subject=$(just -f .flathub.justfile _get_build_subject)

    deps_args="--install-deps-from=flathub"
    if [ "$ref_branch" = "beta" ] || [ "$ref_branch" = "test" ]; then
        deps_args="$deps_args --install-deps-from=flathub-beta"
    fi

    for attempt in 1 2 3 4 5; do
        if flatpak-builder --user $deps_args --install-deps-only builddir "$manifest"; then
            break
        fi
        if [ "$attempt" -eq 5 ]; then
            echo "Failed to install dependencies after $attempt attempts"
            exit 1
        fi
        sleep $((attempt * 15))
    done

    extra_args=""

    if [ "{{build_arch}}" = "x86_64" ]; then
        extra_args="$extra_args --bundle-sources"
    fi

    if [ "$ref_branch" != "test" ]; then
        extra_args="$extra_args --mirror-screenshots-url=https://dl.flathub.org/media --compose-url-policy=full"
    fi

    flatpak-builder -v \
        --force-clean --sandbox --delete-build-dirs \
        --user \
        $extra_args \
        --disable-rofiles-fuse \
        --repo repo \
        --default-branch "$ref_branch" \
        --subject "${subject}" \
        --disable-download \
        --ccache \
        --override-source-date-epoch 1321009871 \
        builddir "$manifest"

install-if-extra-data app_id git_ref:
    #!/usr/bin/env bash
    set -euxo pipefail

    case "{{git_ref}}" in
        "refs/heads/master" | "refs/heads/beta" | refs/heads/branch/*)
            echo "Not a test build, skipping install test"
            exit 0
            ;;
    esac

    if ! grep -qF extra-data builddir/files/manifest.json; then
        echo "No extra-data sources found, skipping install test"
        exit 0
    fi

    echo "Extra-data sources detected, testing install..."

    flatpak remote-add --user --no-gpg-verify local-test repo
    flatpak install -y --user --no-related --no-deps local-test {{app_id}}

validate-build:
    #!/usr/bin/env bash
    set -euxo pipefail

    case "${REF}" in
        "refs/heads/master")
            should_janitor="yes"
            ;;
        *)
            should_janitor="no"
            ;;
    esac

    case "${REF}" in
        "refs/heads/beta")
            exceptions_repo="beta"
            ;;
        refs/pull/*)
            case "${PR_TARGET_BRANCH:-master}" in
                "beta")
                    exceptions_repo="beta"
                    ;;
                *)
                    exceptions_repo="stable"
                    ;;
            esac
            ;;
        *)
            exceptions_repo="stable"
            ;;
    esac

    lint_args=(--gha-format --exceptions --exceptions-repo "$exceptions_repo")

    if [[ "$BUILD_ARCH" == "x86_64" && "$should_janitor" == "yes" ]]; then
        lint_args+=(--janitor-exceptions)
    fi

    [ "${PERMISSION_REVIEW_INTEGRATED:-}" = "true" ] && lint_args+=(--permission-review)

    flatpak-builder-lint "${lint_args[@]}" repo repo

upload url:
    #!/usr/bin/env bash
    set -euxo pipefail

    for attempt in 1 2 3 4 5; do
        if flat-manager-client push "{{url}}" repo; then
            exit 0
        fi
        if [ "$attempt" -eq 5 ]; then
            echo "Failed to upload after $attempt attempts"
            exit 1
        fi
        sleep $((attempt * 15))
    done

prune-cache app_id:
    #!/usr/bin/env bash
    set -euxo pipefail
    manifest=$(just -f .flathub.justfile _get_manifest {{app_id}})
    manifest_json=$(flatpak-builder --show-manifest "$manifest")
    checksums=$(jq -r '.. | objects | (.md5, .sha1, .sha256, .sha512) | strings' <<< "$manifest_json")
    for dir in .flatpak-builder/downloads/*/; do
        [ -d "$dir" ] || continue
        grep -qxF "$(basename "$dir")" <<< "$checksums" || rm -rf "$dir"
    done

    make_absolute() {
        local parent=${1%/} rel=$2
        [[ "$rel" == ../* ]] || { echo "$rel"; return; }
        while [[ "$rel" == ../* ]]; do
            parent=${parent%/*}
            rel=${rel#../}
        done
        echo "$parent/$rel"
    }

    declare -A keep_git=() seen_git=()
    keep_git_mirror() {
        local url=$1 ref=$2 name mirror key sub_url sub_path sub_commit
        name=$(sed -E 's#[/:]+#_#g' <<< "$url")
        keep_git[$name]=1
        [ -z "${seen_git[$name@$ref]:-}" ] || return 0
        seen_git[$name@$ref]=1
        mirror=.flatpak-builder/git/$name
        git -C "$mirror" cat-file -e "$ref:.gitmodules" 2>/dev/null || return 0
        while read -r key sub_url; do
            sub_path=$(git -C "$mirror" config --blob "$ref:.gitmodules" "${key%.url}.path") || continue
            sub_commit=$(git -C "$mirror" ls-tree "$ref" "$sub_path" | awk '$1 == "160000" { print $3 }') || continue
            [ -n "$sub_commit" ] || continue
            keep_git_mirror "$(make_absolute "$url" "$sub_url")" "$sub_commit"
        done < <(git -C "$mirror" config --blob "$ref:.gitmodules" --get-regexp '^submodule\..*\.url$' || true)
    }

    while IFS=$'\t' read -r url ref; do
        keep_git_mirror "$url" "$ref"
    done < <(jq -r '.. | objects | select(.type? == "git" and (.url | type) == "string") | [.url, (.branch // .tag // .commit // "HEAD")] | @tsv' <<< "$manifest_json")

    for dir in .flatpak-builder/git/*/; do
        [ -d "$dir" ] || continue
        [ -n "${keep_git[$(basename "$dir")]:-}" ] || rm -rf "$dir"
    done
    if [ -d .flatpak-builder/ccache ]; then
        find .flatpak-builder/ccache -type f ! -newer .ccache-marker -delete
    fi

show-runtime app_id:
    #!/usr/bin/env bash
    set -euxo pipefail
    git config --global --add safe.directory "*"
    manifest=$(just -f .flathub.justfile _get_manifest {{app_id}})
    flatpak-builder --show-manifest "$manifest" | jq -r '"\(.runtime)-\(."runtime-version")"'
