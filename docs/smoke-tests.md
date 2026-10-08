# Application startup and screenshot checks

App maintainers can opt into checks of the Flatpak that Vorarbeiter just built.
The checks run separately from publishing and never block a build or release.

## Enable checks for an app

Add this object to the app repository's `flathub.json`:

```json
{
  "smoke-test": {
    "enabled": true,
    "screenshot-recipe": "ci/screenshots.yml"
  }
}
```

Preserve existing settings such as `only-arches`. Omit `screenshot-recipe` for
startup verification only. Omit `smoke-test`, or set `enabled` to `false`, to
disable both checks. The supported keys are `enabled` and `screenshot-recipe`;
invalid values produce a configuration error in the check report.

The recipe path is relative to the repository root. It must be a tracked regular
file, without symlinks or parent traversal. Fixture data must also be checked in.
Recipes use the [flatpak-smoke recipe format](https://github.com/razzeee/flatpak-smoke/blob/v0.1.0/docs/reference.md#writing-a-recipe),
including navigation, seeded data, and launch arguments. For example:

```yaml
version: 1
window:
  width: 900
  height: 650
steps:
  - capture:
      name: overview
      caption: Explore the main window
```

The initial implementation supports x86_64 applications. Other architectures are
explicitly skipped; opting in does not change the app's build matrix. Both modes
run as `nobody` with runtime downloads enabled and a five-minute overall timeout
per mode. The complete test job has a 20-minute limit. Vorarbeiter pins the action
and both images centrally; app configuration cannot change those pins or Docker
options.

## Results on pull requests

Vorarbeiter maintains one bot-owned application-check comment per PR. It includes the
tested commit, separate startup and screenshot outcomes, a short diagnostic,
links to the workflow and downloadable results, and up to three inline previews.
Diagnostic screenshots can appear even when a check fails.

Old commits and older build attempts cannot replace newer results. Rerunning the
same commit updates the comment instead of posting another one. Failures caused
by the test infrastructure are reported separately from app failures.

Results, logs, and previews expire after 14 days. The screenshot endpoint checks
that an image belongs to the recorded result artifact before serving it. Expired
artifacts return an unavailable response. These images are review evidence;
maintainers still choose and publish their app-listing screenshots themselves.

Builds without a PR keep their diagnostics in Actions without posting a comment
or opening an issue.

## How it runs

The build workflow uploads the exported OSTree repository and a `git archive`
snapshot of the app's tracked files. Untracked builder state and credentials are
excluded. Input artifacts expire after one day and are limited to 2 GiB. Linked
or special files in the source snapshot are not supported. A preparation or upload
failure does not change the build callback result.
The build records an architecture-specific success output immediately after its
upload, before preparing check inputs. The callback uses those receipts without an API
request; partial reruns can also resolve the latest core build/validation/upload
steps per architecture. A later check-preparation timeout does not turn a completed
upload into a publication failure or cancel other architectures.

Apps without an enabled opt-in keep the existing fail-fast and build-status
behavior. They do not download the input-preparation script or run input uploads.

The `Application startup and screenshot checks` workflow starts when the `Build pipeline` workflow
completes. Only successfully built inputs are tested. It downloads the input for that exact build attempt and
uses the existing repository directly, without rebuilding the app. Startup
verification and recipe capture run independently. This workflow has read-only
GitHub permissions and receives no publishing, callback, or PR-write credentials.

The backend handles the signed completion event, verifies the workflow identity
through GitHub, and maps its source run to a known build pipeline. The app's
artifact cannot select a PR or repository to comment on. Application-check state lives in
`smoke_result`, separate from publication state. Periodic `check-jobs` runs
reconcile up to the 1,000 most recent completed smoke runs to recover missed
events and retry failed notifications. Creation dates are not filtered, so reruns
of older workflows in that window are included.

Result archives are limited to 64 MiB compressed and expanded, 500 entries,
256 KiB per JSON member, and 8 MiB per PNG. The collector retains at most 32 MiB
of files and may omit excess diagnostics. The preview server reads ZIP members
without extracting them, rejects links and path traversal, and keeps a bounded
ten-minute cache. GitHub credentials are used only for the API request, not the
redirected storage download.

## Deploy the integration

1. Apply the database migration with `alembic upgrade head` and deploy the backend.
2. Merge the workflows into Vorarbeiter's default branch. `workflow_run` workflows
   must exist there to receive events.
3. Configure the Vorarbeiter repository's webhook to deliver `workflow_run` events
   to the existing `/api/webhooks/github` endpoint using `GITHUB_WEBHOOK_SECRET`.
   Existing app-repository push/PR webhook subscriptions remain in place.
4. Ensure `GITHUB_ACTIONS_TOKEN` can read Vorarbeiter workflow runs and artifacts,
   and `FLATHUBBOT_TOKEN` can read and update comments on app PRs.
5. Set `BASE_URL` to Vorarbeiter's public HTTPS address so GitHub can fetch inline
   PNG previews. Keep the existing `check-jobs` cron job enabled for reconciliation.
6. Enable an app, run a test build, and verify the check report, previews,
   artifact download, and behavior when the app check fails.

To update flatpak-smoke, change the action commit and image digests together in
`.github/workflows/smoke.yml`, then validate them with an opted-in app.
