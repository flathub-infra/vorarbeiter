from django.contrib import admin
from .models import (
    Provider,
    PipelineTemplate,
    JobTemplate,
    PipelineInstance,
    JobInstance,
)


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "provider_type", "api_base_url", "created_at", "updated_at")
    search_fields = ("name", "provider_type")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("name", "provider_type", "api_base_url")}),
        (
            "Authentication & Configuration",
            {
                "fields": ("credentials", "settings"),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(PipelineTemplate)
class PipelineTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "created_at", "updated_at")
    list_filter = ("name",)
    search_fields = ("name", "description")
    readonly_fields = ("created_at", "updated_at")
    actions = ["run_pipeline"]
    fieldsets = (
        (None, {"fields": ("name", "version", "description")}),
        (
            "Timestamps",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.action(description="Run pipeline")
    def run_pipeline(self, request, queryset):
        import asyncio
        from .models import PipelineInstance, JobInstance
        from .services import PipelineRunner

        count = 0
        for template in queryset:
            pipeline = PipelineInstance.objects.create(
                pipeline_template=template,
                trigger_parameters={
                    "triggered_by": "admin",
                    "user": request.user.username,
                },
            )

            for job_template in template.job_templates.all():
                JobInstance.objects.create(
                    pipeline_instance=pipeline, job_template=job_template
                )

            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError("Event loop is closed")
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            try:
                loop.run_until_complete(PipelineRunner.start_pipeline(pipeline.id))
                count += 1
            except Exception as e:
                self.message_user(
                    request,
                    f"Error starting pipeline {template.name}: {str(e)}",
                    level="ERROR",
                )

        self.message_user(
            request,
            f"Started {count} pipeline(s). Check Pipeline Instances for status.",
        )


class JobTemplateDependencyInline(admin.TabularInline):
    model = JobTemplate.depends_on.through
    fk_name = "from_jobtemplate"
    verbose_name = "Dependency"
    verbose_name_plural = "Dependencies"
    extra = 1


@admin.register(JobTemplate)
class JobTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "pipeline_template", "provider", "created_at")
    list_filter = ("pipeline_template", "provider")
    search_fields = ("name", "description", "pipeline_template__name")
    readonly_fields = ("created_at", "updated_at")
    exclude = ("depends_on",)
    inlines = [JobTemplateDependencyInline]
    fieldsets = (
        (None, {"fields": ("pipeline_template", "name", "description", "provider")}),
        (
            "Provider Configuration",
            {
                "fields": ("provider_config",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(PipelineInstance)
class PipelineInstanceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "pipeline_template",
        "status",
        "created_at",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "pipeline_template")
    search_fields = ("pipeline_template__name",)
    readonly_fields = ("created_at", "started_at", "finished_at")
    fieldsets = (
        (None, {"fields": ("pipeline_template", "status")}),
        (
            "Parameters",
            {
                "fields": ("trigger_parameters",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "started_at", "finished_at"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(JobInstance)
class JobInstanceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "job_template",
        "pipeline_instance",
        "status",
        "created_at",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "job_template", "pipeline_instance__pipeline_template")
    search_fields = ("job_template__name", "pipeline_instance__pipeline_template__name")
    readonly_fields = (
        "callback_id",
        "created_at",
        "queued_at",
        "started_at",
        "finished_at",
    )
    fieldsets = (
        (
            None,
            {"fields": ("pipeline_instance", "job_template", "status", "callback_id")},
        ),
        (
            "Execution Details",
            {
                "fields": (
                    "execution_parameters",
                    "external_job_id",
                    "results",
                    "logs_url",
                ),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "queued_at", "started_at", "finished_at"),
                "classes": ("collapse",),
            },
        ),
    )
