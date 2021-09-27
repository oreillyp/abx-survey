import random
import glob
import math

import pickle
import string

import soundfile
import pandas as pd

import numpy as np
import yaml
import time
import os

from datetime import datetime
import argparse
from os import path
from pathlib import Path
from tqdm import tqdm
from typing import Union, List

import boto3  # for managing MTurk, AWS
import xmltodict

CAESAR_SHIFT = 13  # shift for obfuscating filenames

################################################################################
# Given directory holding experiment results, create survey
################################################################################


def parse_args():
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser(add_help=False)

    parser.add_argument(
        '--config',
        type=str,
        default='config.yaml',
        help='Configuration file for survey actions'
    )

    return parser.parse_args()


def ensure_dir(directory: Union[str, Path]):
    directory = str(directory)
    if len(directory) > 0 and not os.path.exists(directory):
        os.makedirs(directory)


def build_survey_xml(form: dict,
                     n_questions: int,
                     intro: str,
                     outro: str,
                     instructions: str):

    # combine questions in order
    questionnaire = ""
    for question in range(1, n_questions + 1):
        questionnaire += form['questions'][question]['html'] + "<br/><br/>"

    # survey template: intro, replicated questions, outro, instructions
    survey = f"""
        <HTMLQuestion
        xmlns="http://mechanicalturk.amazonaws.com/AWSMechanicalTurkDataSchemas/2011-11-11/HTMLQuestion.xsd">
        <HTMLContent><![CDATA[
        
        <!-- HTML BEGINS HERE -->
        <!DOCTYPE html>
        <html>
        
        <!-- You must include this JavaScript file -->
        <script src="https://assets.crowd.aws/crowd-html-elements.js"></script>

        <!-- For the full list of available Crowd HTML Elements and their input/output documentation,
        please refer to https://docs.aws.amazon.com/sagemaker/latest/dg/sms-ui-template-reference.html -->

        <!-- You must include crowd-form so that your task successfully submit answers -->
        <crowd-form answer-format="flatten-objects">
        
        <!-- introduction & consent -->
        {intro}
        
        <br/>
        <br/>
        <hr/>
        <br/>
        
        <h2>Audio Comparison Test</h2>
        <p>In this task, you will be asked to listen to {n_questions} sets of 
        three short audio recordings (<b>Reference</b>, <b>A</b>, and <b>B</b>). 
        For each set, you will be asked to select which of <b>A</b> or <b>B</b> sounds most like the 
        <b>Reference</b>.</p>
        <br/>


        <!-- questions -->
        {questionnaire}
        
        <!-- closing statement -->
        {outro}
        
        <!-- instructions -->
        {instructions}
        
        </crowd-form>
        
        </html>
        <!-- HTML ENDS HERE -->
        ]]>
        </HTMLContent>
        <FrameHeight>600</FrameHeight>
        </HTMLQuestion>
    """

    return survey


def caesar(s, k):
    """
    Caesar cipher; adapted from https://stackoverflow.com/a/34578873
    """

    upper = {a: chr(a) for a in range(65, 91)}
    lower = {a: chr(a) for a in range(97, 123)}
    digit = {a: chr(a) for a in range(48, 58)}

    for c in s:
        o = ord(c)
        if (o not in upper and o not in lower) or o in digit:
            yield o
        else:
            if o in upper and o + k % 26 in upper:
                yield o + k % 26
            elif o in lower and o + k % 26 in lower:
                yield o + k % 26
            else:
                yield o + k % 26 - 26


def encode_filename(filename: str):
    """
    Apply Caesar cipher to filename (audio URLs remain visible within MTurk)
    """

    # leave extension
    ext = filename.split(".")[-1]
    name = ''.join(filename.split(".")[:-1])

    name = ''.join(map(chr, caesar(name, CAESAR_SHIFT)))

    return name + "." + ext


def decode_filename(filename: str):
    """
    Decode Caesar cipher
    """

    # leave extension
    ext = filename.split(".")[-1]
    name = ''.join(filename.split(".")[:-1])

    name = ''.join(map(chr, caesar(name, -CAESAR_SHIFT)))

    return name + "." + ext


def get_qualification_requirements(min_hits_approved: int,
                                   min_percent_hits_approved: int,
                                   locales_include: List[dict],
                                   locales_exclude: List[dict]
                                   ):

    qualifications = []

    if min_hits_approved is not None:
        qualifications.append(
            {
                'QualificationTypeId': '00000000000000000040',
                'Comparator': 'GreaterThanOrEqualTo',
                'IntegerValues': [min_hits_approved],
                'ActionsGuarded': 'DiscoverPreviewAndAccept'
            }
        )
    if min_percent_hits_approved is not None:
        qualifications.append(
            {
                'QualificationTypeId': '000000000000000000L0',
                'Comparator': 'GreaterThanOrEqualTo',
                'IntegerValues': [min_percent_hits_approved],
                'ActionsGuarded': 'DiscoverPreviewAndAccept'
            }
        )
    if locales_include is not None:
        qualifications.append(
            {
                'QualificationTypeId': '00000000000000000071',
                'Comparator': 'In',
                'LocaleValues': locales_include,
                'ActionsGuarded': 'DiscoverPreviewAndAccept'
            }
        )
    if locales_exclude is not None:
        qualifications.append(
            {
                'QualificationTypeId': '00000000000000000071',
                'Comparator': 'NotIn',
                'LocaleValues': locales_exclude,
                'ActionsGuarded': 'DiscoverPreviewAndAccept'
            }
        )
    return qualifications


def main():

    args = parse_args()

    # load config file
    with open(args.config, 'r') as config:
        config = yaml.safe_load(config)

    # fixed values
    MTURK_REGION = 'us-east-1'  # MTurk requires 'us-east-1' region
    MTURK = f'https://mturk-requester-sandbox.{MTURK_REGION}.amazonaws.com'

    # load client credentials
    credentials = pd.read_csv(config['credentials'])
    AWS_KEY_ID = credentials['Access key ID'].iloc[0]
    AWS_SECRET = credentials['Secret access key'].iloc[0]

    # survey action: `create` or `evaluate`
    ACTION = config['action']

    # local files
    SURVEY_DIR = config['assets_dir']
    AUDIO_DIR = config['audio_dir']
    AUDIO_EXT = config['audio_ext']

    # AWS S3 information
    S3_REGION = 'us-east-1' if not config['s3_region'] else config['s3_region']
    S3_BUCKET = config['s3_bucket']

    # survey details
    TITLE = config['title']
    MAX_QUESTIONS = config['max_questions_per_form']
    DUMMY_QUESTIONS = config['dummy_questions_per_form']
    COVERAGE = config['coverage']
    PAY_PER_HIT = config['reward']
    DESCRIPTION = config['description']
    KEYWORDS = config['keywords']
    LIFETIME = config['lifetime']
    DURATION = config['duration']
    APPROVAL_DELAY = config['approval_delay']

    # check that component files are present in survey directory
    survey_files = [path.basename(p) for p in glob.glob(f'{SURVEY_DIR}/*.html')]
    assert 'instructions.html' in survey_files
    assert 'intro.html' in survey_files
    assert 'outro.html' in survey_files
    assert 'question.html' in survey_files

    # initialize AWS clients (MTurk & S3)
    if config['sandbox']:
        mturk = boto3.client('mturk',
                             region_name=MTURK_REGION,
                             aws_access_key_id=AWS_KEY_ID,
                             aws_secret_access_key=AWS_SECRET,
                             endpoint_url=MTURK
                             )
    else:
        mturk = boto3.client('mturk',
                             region_name=MTURK_REGION,
                             aws_access_key_id=AWS_KEY_ID,
                             aws_secret_access_key=AWS_SECRET
                             )
    s3 = boto3.client('s3',
                      region_name=S3_REGION,
                      aws_access_key_id=AWS_KEY_ID,
                      aws_secret_access_key=AWS_SECRET
                      )

    # load or randomly generate survey ID
    random.seed(datetime.now())
    digits = string.digits
    survey_id = config['survey_id'] if config['survey_id'] else ''.join(
        random.choice(digits) for i in range(6)
    )

    # create new survey
    if ACTION == 'create_new':

        # set output directory
        OUTPUT_DIR = Path(config['output_dir']) / survey_id
        ensure_dir(OUTPUT_DIR)

        # if no S3 bucket is given, create public bucket
        if S3_BUCKET is None:
            s3.create_bucket(
                Bucket=f'survey-{survey_id}',
                ACL='public-read',
                CreateBucketConfiguration={
                    'LocationConstraint': S3_REGION
                }
            )
            S3_BUCKET = f'survey-{survey_id}'

        # check audio files
        audio_reference = list(Path(AUDIO_DIR).rglob(f'reference_*.{AUDIO_EXT}'))
        audio_baseline = list(Path(AUDIO_DIR).rglob(f'baseline_*.{AUDIO_EXT}'))
        audio_proposed = list(Path(AUDIO_DIR).rglob(f'proposed_*.{AUDIO_EXT}'))

        # sort by comparison index (keep files matched)
        audio_reference.sort(key=lambda x: str(x).split("_")[-1], reverse=False)
        audio_baseline.sort(key=lambda x: str(x).split("_")[-1], reverse=False)
        audio_proposed.sort(key=lambda x: str(x).split("_")[-1], reverse=False)

        # determine if 'true' (reference vs. proposed) or 'pseudo' (reference
        # vs. proposed vs. baseline) ABX test
        if len(audio_baseline) == 0:  #
            assert len(audio_reference) == len(audio_proposed)
            ABX_MODE = 'true'
        else:
            assert len(audio_reference) == len(audio_proposed) == len(audio_baseline)
            ABX_MODE = 'pseudo'

        # compute number of forms required for single-coverage of comparisons
        n_audio = MAX_QUESTIONS - DUMMY_QUESTIONS
        n_forms = math.ceil(len(audio_reference) / n_audio)

        # "pad" audio lists with duplicates to fill final form if necessary
        n_pad = n_forms * n_audio - len(audio_reference)
        audio_reference.extend(audio_reference[:n_pad])
        audio_proposed.extend(audio_proposed[:n_pad])
        audio_baseline.extend(audio_baseline[:n_pad])

        # load individual survey question template
        with open(path.join(SURVEY_DIR, 'question.html')) as f:
            question_template = f.read()

        # create survey forms
        print("Generating survey forms & uploading audio")
        forms = []
        for i in tqdm(range(n_forms), total=n_forms):

            form = {
                'form_id': i,
                'caesar_shift': CAESAR_SHIFT,
                'questions': {}
            }

            # select audio for form
            form_reference = audio_reference[i * n_audio: (i + 1)*n_audio]
            form_proposed = audio_proposed[i * n_audio: (i + 1)*n_audio]
            if ABX_MODE == 'pseudo':
                form_baseline = audio_baseline[i * n_audio: (i + 1)*n_audio]

            # assign audio to comparison questions
            question_idx = list(range(MAX_QUESTIONS))
            random.shuffle(question_idx)
            comparison_idx = question_idx[DUMMY_QUESTIONS:]
            dummy_idx = question_idx[:DUMMY_QUESTIONS]

            for j, idx in enumerate(comparison_idx):

                form['questions'][idx + 1] = {
                    'reference': form_reference[j],
                    'proposed': form_proposed[j]
                }
                if ABX_MODE == 'pseudo':
                    form['questions'][idx + 1]['baseline'] = form_baseline[j]

            # assign audio to dummy questions
            for j, idx in enumerate(dummy_idx):

                # randomly select reference audio
                ref_fn = random.choice(form_reference)
                dummy_fn = str(ref_fn).replace('reference', 'dummy')

                # add white noise to obtain dummy audio
                ref_wav, sr = soundfile.read(str(ref_fn))
                mag = np.max(ref_wav)
                noise = np.random.rand(*ref_wav.shape) * .1 * mag - .05 * mag
                dummy_wav = np.clip(ref_wav + noise, a_min=-1, a_max=1)

                # save dummy audio
                soundfile.write(dummy_fn, dummy_wav, sr)

                form['questions'][idx + 1] = {
                    'reference': ref_fn,
                    'dummy': dummy_fn
                }

            # upload all form audio to bucket
            for question in form['questions']:
                for category in form['questions'][question]:
                    file = form['questions'][question][category]

                    # cipher filename and store for recovery
                    cipher_name = encode_filename(path.basename(file))
                    form['questions'][question][category] = cipher_name

                    s3.upload_file(
                        str(file),
                        S3_BUCKET,
                        cipher_name,
                        ExtraArgs={'ACL': 'public-read'}
                    )

            # generate HTML for each question
            for question in form['questions']:

                # randomly assign proposed, reference/baseline to radio buttons
                coin_toss = random.random() > 0.5

                categories = form['questions'][question].keys()
                if 'dummy' in categories:
                    category_a = 'dummy' if coin_toss else 'reference'
                    category_b = 'reference' if coin_toss else 'dummy'
                elif ABX_MODE == 'true':
                    category_a = 'proposed' if coin_toss else 'reference'
                    category_b = 'reference' if coin_toss else 'proposed'
                else:
                    category_a = 'proposed' if coin_toss else 'baseline'
                    category_b = 'baseline' if coin_toss else 'proposed'

                question_html = question_template.format(
                    n_question=question,
                    n_questions=MAX_QUESTIONS,
                    bucket_name=S3_BUCKET,
                    bucket_region=S3_REGION,
                    category_a=category_a,
                    category_b=category_b,
                    audio_a=form['questions'][question][category_a],
                    audio_b=form['questions'][question][category_b],
                    audio_x=form['questions'][question]['reference']
                )

                form['questions'][question]['html'] = question_html

            # load introduction template
            with open(Path(SURVEY_DIR) / 'intro.html') as f:
                intro = f.read()

            # load closing statement template
            with open(Path(SURVEY_DIR) / 'outro.html') as f:
                outro = f.read()

            # load instructions template
            with open(Path(SURVEY_DIR) / 'instructions.html') as f:
                instructions = f.read()

            # generate XML survey template for form
            survey = build_survey_xml(form,
                                      MAX_QUESTIONS,
                                      intro,
                                      outro,
                                      instructions)

            form['final_xml'] = survey
            forms.append(form)

            # log survey XML to output directory
            with open(OUTPUT_DIR / f'survey-{survey_id}-{form["form_id"]}.xml', 'w+') as f:
                f.write(survey)

        # notify user of cost and pause for input
        print(f'Total pay amount: '
              f'${1.4 * COVERAGE * len(forms) * float(PAY_PER_HIT) :.2f} '
              f'({len(forms)} forms, {COVERAGE} assignments per form, '
              f'{PAY_PER_HIT} paid per assignment, 40% Amazon surcharge)')
        print(f'Available prepaid balance: ${mturk.get_account_balance()["AvailableBalance"]}')

        response = input('Finalize HIT creation and charge? [y/n] ')
        if response.lower().strip() == 'y':

            for form in forms:

                # create HIT
                print(f'Creating HIT for form {form["form_id"]}')
                hit = mturk.create_hit(
                    Title=f'{TITLE} ({survey_id}-{form["form_id"]})',
                    Description=DESCRIPTION,
                    Keywords=KEYWORDS,
                    Reward=PAY_PER_HIT,
                    MaxAssignments=COVERAGE,  # number of assignments
                    LifetimeInSeconds=LIFETIME,
                    AssignmentDurationInSeconds=DURATION,
                    AutoApprovalDelayInSeconds=APPROVAL_DELAY,
                    Question=form['final_xml'],
                    QualificationRequirements=get_qualification_requirements(
                        config['qual_min_hits'],
                        config['qual_pct_hits'],
                        config['qual_include_regions'],
                        config['qual_exclude_regions']
                    )
                )
                print(f'Survey form {form["form_id"]} preview link: '
                      f'https://workersandbox.mturk.com/mturk/preview?groupId='
                      f'{hit["HIT"]["HITGroupId"]}')
                form['hit_id'] = hit["HIT"]["HITId"]
                form['hit_group_id'] = hit["HIT"]["HITGroupId"]
                form['survey_id'] = survey_id

            # save all form information (associate forms with HITs)
            with open(OUTPUT_DIR / 'forms.pkl', 'wb') as f:
                pickle.dump(forms, f)

        else:
            print('Exiting; S3 buckets/files and survey files remain')

    # serve from existing forms
    elif ACTION == 'run_existing':

        # set output directory; materials from existing survey are assumed to
        # live here
        OUTPUT_DIR = Path(config['output_dir']) / survey_id
        ensure_dir(OUTPUT_DIR)

        # if no bucket provided, assume default naming
        S3_BUCKET = f'survey-{survey_id}' if not S3_BUCKET else S3_BUCKET

        # load forms from pickle
        forms = None
        raise NotImplementedError()

        # notify user of cost and pause for input
        print(f'Total pay amount: '
              f'${1.4 * COVERAGE * len(forms) * float(PAY_PER_HIT) :.2f} '
              f'({len(forms)} forms, {COVERAGE} assignments per form, '
              f'{PAY_PER_HIT} paid per assignment, 40% Amazon surcharge)')
        print(f'Available prepaid balance: ${mturk.get_account_balance()["AvailableBalance"]}')

        response = input('Finalize HIT creation and charge? [y/n] ')
        if response.lower().strip() == 'y':

            for form in forms:

                # create HIT
                print(f'Creating HIT for form {form["form_id"]}')
                hit = mturk.create_hit(
                    Title=f'{TITLE} ({survey_id}-{form["form_id"]})',
                    Description=DESCRIPTION,
                    Keywords=KEYWORDS,
                    Reward=PAY_PER_HIT,
                    MaxAssignments=COVERAGE,  # number of assignments
                    LifetimeInSeconds=LIFETIME,
                    AssignmentDurationInSeconds=DURATION,
                    AutoApprovalDelayInSeconds=APPROVAL_DELAY,
                    Question=form['final_xml'],
                    QualificationRequirements=get_qualification_requirements(
                        config['qual_min_hits'],
                        config['qual_pct_hits'],
                        config['qual_include_regions'],
                        config['qual_exclude_regions']
                    )
                )
                print(f'Survey form {form["form_id"]} preview link: '
                      f'https://workersandbox.mturk.com/mturk/preview?groupId='
                      f'{hit["HIT"]["HITGroupId"]}')
                form['hit_id'] = hit["HIT"]["HITId"]
                form['hit_group_id'] = hit["HIT"]["HITGroupId"]
                form['survey_id'] = survey_id

    elif ACTION == 'evaluate':

        raise NotImplementedError()

        # summarize active HITs
        response = mturk.list_hits()
        print("Total Active HITs:", response['NumResults'])

        # summarize reviewable HITs
        response = mturk.list_reviewable_hits()
        print("Reviewable HITs:", response['NumResults'])

        # summarize qualifications
        response = mturk.list_qualification_types(
            MustBeOwnedByCaller=True,
            MustBeRequestable=False
        )
        print("Custom qualifications:", response['NumResults'])

    else:
        raise ValueError(f'Invalid survey action {ACTION}')


if __name__ == "__main__":
    main()
