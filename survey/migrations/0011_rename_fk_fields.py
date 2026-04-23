from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('survey', '0010_rename_models'),
    ]

    operations = [
        # SurveyResponse.survey → SurveyResponse.question FIRST
        # (frees up the survey_id column name)
        migrations.RenameField('SurveyResponse', 'survey', 'question'),

        # Now rename survey_set → survey on all models
        migrations.RenameField('Location', 'survey_set', 'survey'),
        migrations.RenameField('Question', 'survey_set', 'survey'),
        migrations.RenameField('Incentive', 'survey_set', 'survey'),
        migrations.RenameField('SurveyResponse', 'survey_set', 'survey'),
    ]