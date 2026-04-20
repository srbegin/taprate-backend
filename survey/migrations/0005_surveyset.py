"""
Migration: introduce SurveySet model and migrate existing data.

Steps:
  1. Create SurveySet table
  2. Add Survey.survey_set FK + Survey.position
  3. Data migration: wrap each existing Survey in its own SurveySet
  4. Add Location.survey_set FK
  5. Data migration: point Location.survey_set to the right set
  6. Add SurveyResponse.session_id + SurveyResponse.survey_set FK
  7. Remove Survey fields that moved to SurveySet
     (comments_enabled, comments_prompt, alert_threshold)
  8. Remove Location.survey FK (replaced by survey_set)

Depends on: 0003_add_alert_fields
!! Update the dependencies line below to match your actual migration filename !!
"""

import uuid as uuid_module
from django.db import migrations, models
import django.db.models.deletion


def wrap_surveys_in_sets(apps, schema_editor):
    """Create a SurveySet for every existing Survey and link them."""
    Survey = apps.get_model('survey', 'Survey')
    SurveySet = apps.get_model('survey', 'SurveySet')

    for survey in Survey.objects.select_related('organization').all():
        survey_set = SurveySet.objects.create(
            id=uuid_module.uuid4(),
            organization=survey.organization,
            name=survey.name,
            comments_enabled=getattr(survey, 'comments_enabled', False),
            comments_prompt=getattr(survey, 'comments_prompt', 'Any additional feedback?'),
            alert_threshold=getattr(survey, 'alert_threshold', 2),
        )
        survey.survey_set = survey_set
        survey.position = 0
        survey.save()


def migrate_location_survey_fks(apps, schema_editor):
    """Point Location.survey_set at the SurveySet wrapping Location.survey."""
    Location = apps.get_model('survey', 'Location')

    for location in Location.objects.select_related('survey__survey_set').all():
        if location.survey_id and location.survey.survey_set_id:
            location.survey_set = location.survey.survey_set
            location.save()


def migrate_response_survey_sets(apps, schema_editor):
    """Backfill SurveyResponse.survey_set from each response's survey."""
    SurveyResponse = apps.get_model('survey', 'SurveyResponse')

    for resp in SurveyResponse.objects.select_related('survey__survey_set').all():
        if resp.survey_id and resp.survey.survey_set_id:
            resp.survey_set = resp.survey.survey_set
            resp.save()


class Migration(migrations.Migration):

    # !! Update to match your latest migration filename !!
    dependencies = [
        ('survey', '0004_add_alert_fields'),
    ]

    operations = [
        # ── 1. Create SurveySet ───────────────────────────────────────────────
        migrations.CreateModel(
            name='SurveySet',
            fields=[
                ('id', models.UUIDField(primary_key=True, default=uuid_module.uuid4, editable=False, serialize=False)),
                ('name', models.CharField(max_length=200)),
                ('comments_enabled', models.BooleanField(default=False)),
                ('comments_prompt', models.CharField(blank=True, default='Any additional feedback?', max_length=200)),
                ('alert_threshold', models.IntegerField(default=2)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('organization', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='survey_sets',
                    to='survey.organization',
                )),
            ],
        ),

        # ── 2. Add Survey.survey_set FK + position ────────────────────────────
        migrations.AddField(
            model_name='survey',
            name='survey_set',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='surveys',
                to='survey.surveyset',
            ),
        ),
        migrations.AddField(
            model_name='survey',
            name='position',
            field=models.IntegerField(default=0),
        ),

        # ── 3. Data migration: wrap each Survey in a SurveySet ────────────────
        migrations.RunPython(wrap_surveys_in_sets, migrations.RunPython.noop),

        # ── 4. Add Location.survey_set FK ─────────────────────────────────────
        migrations.AddField(
            model_name='location',
            name='survey_set',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='locations',
                to='survey.surveyset',
            ),
        ),

        # ── 5. Data migration: update Location.survey_set ────────────────────
        migrations.RunPython(migrate_location_survey_fks, migrations.RunPython.noop),

        # ── 6. Add SurveyResponse.session_id + survey_set ────────────────────
        migrations.AddField(
            model_name='surveyresponse',
            name='session_id',
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='surveyresponse',
            name='survey_set',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='responses',
                to='survey.surveyset',
            ),
        ),

        # ── 7. Data migration: backfill SurveyResponse.survey_set ────────────
        migrations.RunPython(migrate_response_survey_sets, migrations.RunPython.noop),

        # ── 8. Remove fields that moved from Survey → SurveySet ──────────────
        migrations.RemoveField(model_name='survey', name='comments_enabled'),
        migrations.RemoveField(model_name='survey', name='comments_prompt'),
        migrations.RemoveField(model_name='survey', name='alert_threshold'),
        migrations.RemoveField(model_name='survey', name='name'),

        # ── 9. Remove Location.survey FK (replaced by survey_set) ────────────
        migrations.RemoveField(model_name='location', name='survey'),
    ]