import logging
import json
import os
import sys
from os.path import join, abspath, dirname
from lib.db.models import MeritocracyMentioned, User

import settings
import github_api as gh

THIS_DIR = dirname(abspath(__file__))

__log = logging.getLogger("pull_requests")


def poll_pull_requests(api):
    __log.info("looking for PRs")

    # get voting window
    base_voting_window = gh.voting.get_initial_voting_window()

    # get all ready prs (disregarding of the voting window)
    prs = gh.prs.get_ready_prs(api, settings.URN, 0)

    top_contributors = sorted(gh.repos.get_contributors(api, settings.URN),
                              key=lambda user: user["total"], reverse=True)
    top_contributors = [item["author"]["login"].lower() for item in top_contributors]
    contributors = set(top_contributors)  # store it while it's still a complete list
    top_contributors = top_contributors[:settings.MERITOCRACY_TOP_CONTRIBUTORS]
    top_contributors = set(top_contributors)
    top_voters = (User
                  .select()
                  .order_by(User.votes.desc())
                  .limit(settings.MERITOCRACY_TOP_VOTERS)
                  .get())
    top_voters = set(map(lambda voter: voter.username, top_voters))
    meritocracy = top_voters | top_contributors
    __log.info("generated meritocracy: " + str(meritocracy))

    with open('server/meritocracy.json', 'w') as mfp:
        json.dump(list(meritocracy), mfp)

    needs_update = False
    for pr in prs:
        pr_num = pr["number"]
        __log.info("processing PR #%d", pr_num)

        # gather all current votes
        votes, meritocracy_satisfied = gh.voting.get_votes(api, settings.URN, pr, meritocracy)

        # is our PR approved or rejected?
        vote_total, variance = gh.voting.get_vote_sum(api, votes, contributors)
        threshold = gh.voting.get_approval_threshold(api, settings.URN)
        is_approved = vote_total >= threshold and meritocracy_satisfied

        seconds_since_updated = gh.prs.seconds_since_updated(api, pr)

        voting_window = base_voting_window
        # the PR is mitigated or the threshold is not reached ?
        if variance >= threshold or not is_approved:
            voting_window = gh.voting.get_extended_voting_window(api, settings.URN)
            if (settings.IN_PRODUCTION and vote_total >= threshold / 2 and
                    seconds_since_updated > base_voting_window and not meritocracy_satisfied):
                # check if we need to mention the meritocracy
                try:
                    commit = pr["head"]["sha"]

                    mm, created = MeritocracyMentioned.get_or_create(commit_hash=commit)
                    if created:
                        meritocracy_mentions = meritocracy - {pr["user"]["login"].lower(),
                                                              "chaosbot"}
                        gh.comments.leave_meritocracy_comment(api, settings.URN, pr["number"],
                                                              meritocracy_mentions)
                except:
                    __log.exception("Failed to process meritocracy mention")

        # is our PR in voting window?
        in_window = seconds_since_updated > voting_window

        if is_approved:
            __log.info("PR %d status: will be approved", pr_num)

            gh.prs.post_accepted_status(
                api, settings.URN, pr, seconds_since_updated, voting_window, votes, vote_total,
                threshold, meritocracy_satisfied)

            if in_window:
                __log.info("PR %d approved for merging!", pr_num)

                try:
                    sha = gh.prs.merge_pr(api, settings.URN, pr, votes, vote_total,
                                          threshold, meritocracy_satisfied)
                # some error, like suddenly there's a merge conflict, or some
                # new commits were introduced between finding this ready pr and
                # merging it
                except gh.exceptions.CouldntMerge:
                    __log.info("couldn't merge PR %d for some reason, skipping",
                               pr_num)
                    gh.issues.label_issue(api, settings.URN, pr_num, ["can't merge"])
                    continue

                gh.comments.leave_accept_comment(
                    api, settings.URN, pr_num, sha, votes, vote_total,
                    threshold, meritocracy_satisfied)
                gh.issues.label_issue(api, settings.URN, pr_num, ["accepted"])

                # chaosbot rewards merge owners with a follow
                pr_owner = pr["user"]["login"]
                gh.users.follow_user(api, pr_owner)

                needs_update = True

        else:
            __log.info("PR %d status: will be rejected", pr_num)

            if in_window:
                gh.prs.post_rejected_status(
                    api, settings.URN, pr, seconds_since_updated, voting_window, votes,
                    vote_total, threshold, meritocracy_satisfied)
                __log.info("PR %d rejected, closing", pr_num)
                gh.comments.leave_reject_comment(
                    api, settings.URN, pr_num, votes, vote_total, threshold,
                    meritocracy_satisfied)
                gh.issues.label_issue(api, settings.URN, pr_num, ["rejected"])
                gh.prs.close_pr(api, settings.URN, pr)
            elif vote_total < 0:
                gh.prs.post_rejected_status(
                    api, settings.URN, pr, seconds_since_updated, voting_window, votes,
                    vote_total, threshold, meritocracy_satisfied)
            else:
                gh.prs.post_pending_status(
                    api, settings.URN, pr, seconds_since_updated, voting_window, votes,
                    vote_total, threshold, meritocracy_satisfied)

        for username in votes:
            # TODO keep track of user ID
            user, created = User.get_or_create(login=username, defaults={"votes": 1})
            if not created:
                user.votes += 1
                user.save()

    # we approved a PR, restart
    if needs_update:
        __log.info("updating code and requirements and restarting self")
        startup_path = join(THIS_DIR, "..", "startup.sh")

        # before we exec, we need to flush i/o buffers so we don't lose logs or voters
        sys.stdout.flush()
        sys.stderr.flush()

        os.execl(startup_path, startup_path)

    __log.info("Waiting %d seconds until next scheduled PR polling event",
               settings.PULL_REQUEST_POLLING_INTERVAL_SECONDS)
