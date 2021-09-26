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
        You will be asked to transcribe the <b>Reference</b> recording, and then 
        to select which of <b>A</b> or <b>B</b> sounds most like the 
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

    # leave extension
    ext = filename.split(".")[-1]
    name = ''.join(filename.split(".")[:-1])

    name = ''.join(map(chr, caesar(name, CAESAR_SHIFT)))

    return name + "." + ext


def decode_filename(filename: str):

    # leave extension
    ext = filename.split(".")[-1]
    name = ''.join(filename.split(".")[:-1])

    name = ''.join(map(chr, caesar(name, -CAESAR_SHIFT)))

    return name + "." + ext


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

    # initialize AWS clients
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

    if ACTION == 'create':

        # randomly generate survey ID
        random.seed(datetime.now())
        digits = string.digits
        survey_id = ''.join(random.choice(digits) for i in range(6))

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

        # sort by comparison index
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
                ref_wav, sr = soundfile.read(ref_fn)
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

            # log survey XML
            with open(Path(AUDIO_DIR) / f'survey-{survey_id}-{form["id"]}.xml', 'w+') as f:
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
                print(f'Creating HIT for form {form["id"]}')
                hit = mturk.create_hit(
                    Title=f'{TITLE} ({survey_id}-{form["id"]})',
                    Description=DESCRIPTION,
                    Keywords=KEYWORDS,
                    Reward=PAY_PER_HIT,
                    MaxAssignments=COVERAGE,  # number of assignments
                    LifetimeInSeconds=LIFETIME,
                    AssignmentDurationInSeconds=DURATION,
                    AutoApprovalDelayInSeconds=APPROVAL_DELAY,
                    Question=form['final_xml']
                )
                print(f'Survey form {form["id"]} preview link: '
                      f'https://workersandbox.mturk.com/mturk/preview?groupId='
                      f'{hit["HIT"]["HITGroupId"]}')
                form['hit_id'] = hit["HIT"]["HITId"]
                form['hit_group_id'] = hit["HIT"]["HITGroupId"]
                form['survey_id'] = survey_id

            # save all form information (associate forms with HITs)
            with open(Path(AUDIO_DIR) / 'forms.pkl', 'wb') as f:
                pickle.dump(forms, f)

        else:
            print('Exiting; S3 buckets/files and survey files remain')
            exit()

        exit()

        # set questionnaire size: as close to MAX_QUESTIONS as possible while dividing cleanly
        # try to make sure you fix the divisibility/padding scenario ahead of time with number of
        # real and dummy questions... maybe even hard-code so duplicates/padding is never an issue?

        # TODO: set sensible defaults for pay, maybe change to per-question computed value where
        # scipt user only specifies pay per question? check in on other common settings
        # in academia/industry (e.g. qualifications, auto-approve delay)

        # TODO: associate qualification with worker
        # mturk.associate_qualification_with_worker
        # TODO: do titles need to be unique to avoid auto-batching / duplicate workers?
        # TODO: add QualificationRequirements parameter to HIT creation


    elif ACTION == 'evaluate':

        # TODO: accept, reject, bonus

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


def parse_HIT_results(client: boto3.client, hit_id: str):
    """
    Given MTurk client object and HIT ID, tabulate assignment results for HIT
    and save to .csv file
    """
    assignments = client.list_assignments_for_hit(
        HITId=hit_id,
        AssignmentStatuses=['Submitted']
    )['Assignments']

    for assignment in assignments:

        info = f"""
        \n------------------------------------------------------------\n
        Assignment ID: {assignment['AssignmentId']}
        Worker ID: {assignment['WorkerId']}\n
        Status: {assignment['AssignmentStatus']}
        Submission Time: {assignment['SubmitTime']}
        Auto-Approval Time: {assignment['AutoApprovalTime']}
        """
        print(info)

        answers = xmltodict.parse(assignment['Answer'])

        if isinstance(answers['QuestionFormAnswers']['Answer'], List):
            for answer in answers['QuestionFormAnswers']['Answer']:
                result = f"""
                Field: {answer['QuestionIdentifier']}
                Response: {answer['FreeText']}
                """
                print(result)
        else:
            result = f"""
                Field: {answers['QuestionFormAnswers']['Answer']['QuestionIdentifier']}
                Response: {answers['QuestionFormAnswers']['Answer']['FreeText']}
            """
            print(result)

        # use RegEx to parse all ABX questions

    # TODO: save to CSV file; start by grabbing headers from HIT answer fields



if __name__ == "__main__":
    main()
