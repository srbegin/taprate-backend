from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('survey', '0009_remove_incentive_survey_incentive_name_and_more'),
    ]

    operations = [
        migrations.RenameModel('Survey', 'Question'),
        migrations.RenameModel('SurveySet', 'Survey'),
    ]