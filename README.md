# MTurk ABX Utility

This script performs the following simple tasks:
* Given survey parameters and labeled audio, generate ABX (reference vs. proposed) or pseudo-ABX (reference vs. baseline vs. proposed) survey forms using a collection of editable templates
* Obfuscate, link, and upload corresponding audio files to an AWS S3 bucket
* Launch surveys as MTurk HITs
* Compile survey results and compute basic statistics

## Installation

Clone this repository:

```
git clone https://github.com/oreillyp/abx-survey.git
```

Install dependencies:

```
pip install -r requirements.txt
```

## Quick Start

1. If you want to perform a true ABX test, label all audio file pairs as 
   ```
   reference_1.ext
   proposed_1.ext
   reference_2.ext
   proposed_2.ext
   ...
   ```
   where `ext` is any audio file extension. Place all these files in a directory, and edit `config.yaml` so that `audio_dir` points to this directory and `ext` matches your file extensions. If you want to perform a pseudo-ABX test, instead label your file triplets as 
   ```
   reference_1.ext
   baseline_1.ext
   proposed_1.ext
   reference_2.ext
   baseline_2.ext
   proposed_2.ext
   ...
   ```
   
2. If you do not already have linked [AWS](https://aws.amazon.com/account/) and [MTurk Requester](https://requester.mturk.com/) accounts, follow [these instructions](https://docs.aws.amazon.com/AWSMechTurk/latest/AWSMechanicalTurkGettingStartedGuide/SetUp.html).
3. Set up an [IAM user](https://docs.aws.amazon.com/AWSMechTurk/latest/AWSMechanicalTurkGettingStartedGuide/SetUp.html#create-iam-user-or-role) with `s3fullaccess` and `amazonmechanicalturkfullaccess` permissions. For a brief tutorial on setting IAM permissions, see [here](https://www.youtube.com/watch?v=SmilJDG4B_8).
4. Download your IAM user credentials as a `.csv` and edit `config.yaml` so that `credentials` points to this file
5. Edit the details of your survey in `config.yaml` (see table below, from `title` onwards).
6. Run `python survey.py` to generate your survey in sandbox mode
7. Preview your survey. As of 2017, HITs created via Amazon's `boto3` SDK are not viewable within the MTurk web interface. To access and manage your HITs within a GUI, simply download [this file](https://raw.githubusercontent.com/jtjacques/mturk-manage/master/mturk-manage.html) from the [MTurk-Manage](https://github.com/jtjacques/mturk-manage) repository and open it in any browser (you will have to provide the user credentials from your `.csv` file).


Regardless of whether you launch a survey in sandbox mode or MTurk proper, the script will produce the following output:
  * the XML questionnaire for each survey form will be saved to `output/`
  * any 'dummy' audio generated for the survey will be saved to `audio_dir/`
  * a list of dictionaries, each holding the contents of a survey form, will be pickled and saved as `forms.pkl` in `output/`
  * all audio used in the survey will be uploaded to the S3 bucket with obfuscated filenames


## Configuration

The driver script `create_survey.py` accepts the command-line argument `config`; this must be a path to a `.yaml` configuration file with the following fields:

| Field | Default Value | Description |
|---|---|---|
| `action` | `create_new` | must be one of `create_new`, `run_existing`, `evaluate`|
| `sandbox` | `true` | If `true`, create/evaluate surveys in the MTurk Sandbox environment. It is strongly recommended that you test surveys in the sandbox environment before launching with MTurk proper |
|`credentials` | `credentials.csv` | a `.csv` file holding AWS client credentials. AWS user agent should be configured with `s3fullaccess` and `amazonmechanicalturkfullaccess` permissions, and the file should contain the fields `Access key ID` and `Secret access key` |
| `s3_region` | `us-east-2` | AWS S3 bucket region |
| `s3_bucket` | `None` | name of existing AWS S3 bucket to use; if `None`, a new bucket will be created |
| `survey_id` | `None` | identifier for survey; if `None`, will be a randomly generated string of digits. When running with the action `run_existing`, provide the ID of your existing survey here |
| `audio_dir` | `audio` | path to directory containing survey audio. All files must be named descriptively (`reference_*.ext` or `proposed_*.ext` for a true ABX test; `reference_*.ext`, `baseline_*.ext`, or `proposed_*.ext` for a two-way pseudo-ABX test)|
| `audio_ext` | `wav` | audio file extension |
| `assets_dir`| `assets` | directory from which to load HTML survey templates |
| `output_dir` | `output` | directory to which outputs are saved |
| `title` | `None` | title of survey; this is what MTurk workers will see |
| `description` | `None` | short description of survey; this is what MTurk workers will see before they decide to accept the survey |
| `keywords`  | `'audio, comparison'` | survey descriptors |
| `reward` | `3.00` | pay for completion of a single survey (HIT), in dollars |
| `lifetime` | `345600` | amount of time survey remains available, in seconds |
|`duration` | `1800` | amount of time workers have to complete survey, in seconds |
| `approval_delay` | `259200` | amount of time after completion of survey until work is automatically approved and workers are paid |
| `max_questions_per_form` | `20` | maximum number of questions a worker will be asked to answer in a single survey |
| `dummy_questions_per_form` | `4` | number of "listening-check" questions (using a white-noise comparison) inserted into each survey |
| `coverage` | `1` | number of times each audio file will be evaluated; analogously, the number of workers who can complete each survey form |
| `qual_min_hits` | `None` | number of accepted HITs required to view and submit survey (integer in \[1, 100\]). If `None`, has no effect |
| `qual_pct_hits` | `None` | percentage of HITs accepted required to view and submit survey (integer in \[1, 100\]). If `None`, has no effect|
| `qual_exclude_regions` | `None` | List of locales to exclude; each locale must be a dictionary with the [required format](https://docs.aws.amazon.com/AWSMechTurk/latest/AWSMturkAPI/ApiReference_LocaleDataStructureArticle.html). If `None`, has no effect |
| `qual_include_regions` | `None` | List of locales to include (all other locales will be rejected); each locale must be a dictionary with the [required format](https://docs.aws.amazon.com/AWSMechTurk/latest/AWSMturkAPI/ApiReference_LocaleDataStructureArticle.html). If `None`, has no effect |


## To-Do

- [ ] add `evaluate` functionality
- [ ] increase radio button size
- [ ] check on capping requests to public S3 bucket or using pre-signed URLs
- [ ] manage unique workers (explicit instructions or automatic qualification; either may require a continuously-running script to pull worker/assignment information)