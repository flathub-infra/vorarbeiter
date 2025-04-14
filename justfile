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

checkout repo ref:
    #!/usr/bin/env bash
    set -euxo pipefail
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

validate-manifest app_id:
    #!/usr/bin/env bash
    set -euxo pipefail
    manifest=$(just _get_manifest {{app_id}})
    flatpak-builder-lint --exceptions manifest "$manifest"

build app_id branch="stable":
    #!/usr/bin/env bash
    set -euxo pipefail

    manifest=$(just _get_manifest {{app_id}})
    subject=$(just _get_build_subject)

    deps_args="--install-deps-from=flathub"
    if [ "{{branch}}" = "beta" ] || [ "{{branch}}" = "test" ]; then
        deps_args="$deps_args --install-deps-from=flathub-beta"
    fi

    flatpak-builder -v \
        --force-clean --sandbox --delete-build-dirs \
        --user $deps_args \
        --disable-rofiles-fuse \
        --mirror-screenshots-url=https://dl.flathub.org/media \
        --repo repo \
        --extra-sources=./downloads \
        --default-branch "{{branch}}" \
        --subject "${subject}" \
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
