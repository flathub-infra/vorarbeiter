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
    echo "$commit_msg ($commit_hash)"

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
        appid = None

        for ext in ("yml", "yaml", "json"):
            files.extend(glob.glob(f"{dirname}/*.{ext}"))

        for filename in files:
            if os.path.isfile(filename):
                ext = filename.split(".")[-1]

                with open(filename) as f:
                    if ext in ("yml", "yaml"):
                        manifest = yaml.safe_load(f)
                        if "app-id" in manifest:
                            appid = manifest["app-id"]
                        elif "id" in manifest:
                            appid = manifest["id"]
                    else:
                        parser = Json.Parser()
                        if parser.load_from_file(filename):
                            root_node = parser.get_root()
                            if root_node.get_node_type() == Json.NodeType.OBJECT:
                                json_object = root_node.get_object()
                                if json_object.has_member("id"):
                                    appid = json_object.get_string_member("id")
                                elif json_object.has_member("app-id"):
                                    appid = json_object.get_string_member("app-id")

                if not appid:
                    continue

                if appid:
                    manifest_file = os.path.basename(filename)
                    if os.path.splitext(manifest_file)[0] != appid:
                        continue
                    ret = (manifest_file, appid)

        return ret


    path = os.environ.get("path")

    manifest_file, appid = detect_appid(path)
    if manifest_file is None or appid is None:
        print("Failed to detect appid")
        sys.exit(1)

    print(appid)

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
    flatpak-builder-lint --exceptions manifest "$manifest"

download-sources app_id:
    #!/usr/bin/env bash
    set -euxo pipefail

    manifest=$(just -f .flathub.justfile _get_manifest {{app_id}})

    max_retries=5
    sleep_seconds=7

    for (( retry_count=0; retry_count<$max_retries; retry_count++ )); do
        if flatpak-builder --force-clean --sandbox --download-only builddir "$manifest"; then
            exit 0
        fi

        if [[ $retry_count -lt $((max_retries - 1)) ]]; then
            echo "Attempt $((retry_count + 1)) failed. Retrying in $sleep_seconds seconds..."
            sleep $sleep_seconds
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

    if [ "{{build_arch}}" = "x86_64" ]; then
        deps_args="$deps_args --bundle-sources"
    fi

    flatpak-builder -v \
        --force-clean --sandbox --delete-build-dirs \
        --user $deps_args \
        --disable-rofiles-fuse \
        --mirror-screenshots-url=https://dl.flathub.org/media \
        --repo repo \
        --default-branch "$ref_branch" \
        --subject "${subject}" \
        --disable-download \
        --ccache \
        --override-source-date-epoch 1321009871 \
        builddir "$manifest"

commit-screenshots:
    #!/usr/bin/env bash
    set -euxo pipefail
    mkdir -p builddir/files/share/app-info/media
    ostree commit --repo=repo --canonical-permissions --branch=screenshots/{{arch()}} builddir/files/share/app-info/media

validate-build:
    #!/usr/bin/env bash
    set -euxo pipefail
    flatpak-builder-lint --exceptions repo repo

generate-deltas:
    #!/usr/bin/env bash
    set -euxo pipefail
    flatpak build-update-repo --generate-static-deltas --static-delta-ignore-ref=*.Debug --static-delta-ignore-ref=*.Sources repo

upload url:
    #!/usr/bin/env bash
    set -euxo pipefail
    flat-manager-client push "{{url}}" repo

show-runtime app_id:
    #!/usr/bin/env bash
    set -euxo pipefail
    git config --global --add safe.directory "*"
    manifest=$(just -f .flathub.justfile _get_manifest {{app_id}})
    flatpak-builder --show-manifest "$manifest" | jq -r '"\(.runtime)-\(."runtime-version")"'
