// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MilestoneCrowdfund} from "../src/MilestoneCrowdfund.sol";

/// @dev Backer contract that tries to re-enter claimRefund when it receives ETH.
contract ReentrantBacker {
    MilestoneCrowdfund public immutable target;
    uint256 public campaignId;
    bool public attacking;

    constructor(MilestoneCrowdfund target_) {
        target = target_;
    }

    function back(uint256 id) external payable {
        campaignId = id;
        target.contribute{value: msg.value}(id);
    }

    function attack() external {
        attacking = true;
        target.claimRefund(campaignId);
    }

    receive() external payable {
        if (attacking) {
            attacking = false;
            target.claimRefund(campaignId);
        }
    }
}

contract MilestoneCrowdfundTest is Test {
    MilestoneCrowdfund internal cf;

    address internal creator = makeAddr("creator");
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");
    address internal carol = makeAddr("carol");
    address internal stranger = makeAddr("stranger");

    uint128 internal constant GOAL = 10 ether;
    uint32 internal constant FUNDING = 7 days;
    uint32 internal constant VOTING = 3 days;
    uint32 internal constant WINDOW = 30 days;
    bytes32 internal constant META = keccak256("campaign metadata");
    bytes32 internal constant PROOF = keccak256("milestone proof");

    event CampaignCreated(
        uint256 indexed campaignId,
        address indexed creator,
        uint256 goal,
        uint64 deadline,
        uint8 milestoneCount,
        bytes32 metadataHash
    );
    event Contributed(uint256 indexed campaignId, address indexed backer, uint256 amount, uint256 totalRaised);
    event Refunded(uint256 indexed campaignId, address indexed backer, uint256 amount);

    function setUp() public {
        cf = new MilestoneCrowdfund();
        vm.deal(alice, 100 ether);
        vm.deal(bob, 100 ether);
        vm.deal(carol, 100 ether);
        vm.deal(stranger, 100 ether);
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    function _pcts3() internal pure returns (uint16[] memory p) {
        p = new uint16[](3);
        p[0] = 3000;
        p[1] = 3000;
        p[2] = 4000;
    }

    function _create(uint16[] memory pcts) internal returns (uint256 id) {
        vm.prank(creator);
        id = cf.createCampaign(GOAL, FUNDING, VOTING, WINDOW, pcts, META);
    }

    function _back(uint256 id, address who, uint256 amount) internal {
        vm.prank(who);
        cf.contribute{value: amount}(id);
    }

    /// @dev alice 6, bob 3, carol 1 => 10 ether, goal reached, campaign Active.
    function _fundedCampaign() internal returns (uint256 id) {
        id = _create(_pcts3());
        _back(id, alice, 6 ether);
        _back(id, bob, 3 ether);
        _back(id, carol, 1 ether);
        vm.warp(block.timestamp + FUNDING);
        cf.finalizeFunding(id);
    }

    function _submit(uint256 id) internal {
        vm.prank(creator);
        cf.submitMilestone(id, PROOF);
    }

    function _vote(uint256 id, address who, bool approve) internal {
        vm.prank(who);
        cf.vote(id, approve);
    }

    function _closeVote(uint256 id) internal {
        vm.warp(block.timestamp + VOTING);
        cf.finalizeMilestone(id);
    }

    function _approveCurrent(uint256 id) internal {
        _submit(id);
        _vote(id, alice, true);
        _closeVote(id);
    }

    function _rejectCurrent(uint256 id) internal {
        _submit(id);
        _vote(id, alice, false);
        _closeVote(id);
    }

    function _state(uint256 id) internal view returns (MilestoneCrowdfund.CampaignState) {
        return cf.getCampaign(id).state;
    }

    // ------------------------------------------------------------------
    // createCampaign
    // ------------------------------------------------------------------

    function test_CreateCampaign_StoresParameters() public {
        vm.expectEmit(true, true, false, true);
        emit CampaignCreated(0, creator, GOAL, uint64(block.timestamp + FUNDING), 3, META);
        uint256 id = _create(_pcts3());

        MilestoneCrowdfund.Campaign memory c = cf.getCampaign(id);
        assertEq(id, 0);
        assertEq(cf.campaignCount(), 1);
        assertEq(c.creator, creator);
        assertEq(c.goal, GOAL);
        assertEq(c.deadline, block.timestamp + FUNDING);
        assertEq(c.milestoneCount, 3);
        assertEq(c.metadataHash, META);
        assertEq(uint8(c.state), uint8(MilestoneCrowdfund.CampaignState.Funding));

        MilestoneCrowdfund.Milestone[] memory ms = cf.getMilestones(id);
        assertEq(ms.length, 3);
        assertEq(ms[0].percentageBps, 3000);
        assertEq(ms[2].percentageBps, 4000);
    }

    function test_RevertWhen_GoalIsZero() public {
        vm.expectRevert(MilestoneCrowdfund.InvalidGoal.selector);
        cf.createCampaign(0, FUNDING, VOTING, WINDOW, _pcts3(), META);
    }

    function test_RevertWhen_DurationOutOfRange() public {
        vm.expectRevert(MilestoneCrowdfund.InvalidDuration.selector);
        cf.createCampaign(GOAL, 1 minutes, VOTING, WINDOW, _pcts3(), META);
        vm.expectRevert(MilestoneCrowdfund.InvalidDuration.selector);
        cf.createCampaign(GOAL, 91 days, VOTING, WINDOW, _pcts3(), META);
        vm.expectRevert(MilestoneCrowdfund.InvalidDuration.selector);
        cf.createCampaign(GOAL, FUNDING, 15 days, WINDOW, _pcts3(), META);
        vm.expectRevert(MilestoneCrowdfund.InvalidDuration.selector);
        cf.createCampaign(GOAL, FUNDING, VOTING, 1, _pcts3(), META);
    }

    function test_RevertWhen_MilestoneCountInvalid() public {
        vm.expectRevert(MilestoneCrowdfund.InvalidMilestones.selector);
        cf.createCampaign(GOAL, FUNDING, VOTING, WINDOW, new uint16[](0), META);

        uint16[] memory six = new uint16[](6);
        for (uint256 i; i < 6; ++i) {
            six[i] = 1000;
        }
        six[5] = 5000;
        vm.expectRevert(MilestoneCrowdfund.InvalidMilestones.selector);
        cf.createCampaign(GOAL, FUNDING, VOTING, WINDOW, six, META);
    }

    function test_RevertWhen_PercentagesDoNotSumTo100() public {
        uint16[] memory p = new uint16[](2);
        p[0] = 5000;
        p[1] = 4000;
        vm.expectRevert(MilestoneCrowdfund.InvalidPercentages.selector);
        cf.createCampaign(GOAL, FUNDING, VOTING, WINDOW, p, META);
    }

    function test_RevertWhen_PercentageIsZero() public {
        uint16[] memory p = new uint16[](2);
        p[0] = 10000;
        p[1] = 0;
        vm.expectRevert(MilestoneCrowdfund.InvalidPercentages.selector);
        cf.createCampaign(GOAL, FUNDING, VOTING, WINDOW, p, META);
    }

    function test_RevertWhen_MetadataHashEmpty() public {
        vm.expectRevert(MilestoneCrowdfund.InvalidHash.selector);
        cf.createCampaign(GOAL, FUNDING, VOTING, WINDOW, _pcts3(), bytes32(0));
    }

    // ------------------------------------------------------------------
    // contribute / finalizeFunding / cancel
    // ------------------------------------------------------------------

    function test_Contribute_TracksTotalsAndBackers() public {
        uint256 id = _create(_pcts3());
        vm.expectEmit(true, true, false, true);
        emit Contributed(id, alice, 2 ether, 2 ether);
        _back(id, alice, 2 ether);
        _back(id, alice, 1 ether);
        _back(id, bob, 4 ether);

        MilestoneCrowdfund.Campaign memory c = cf.getCampaign(id);
        assertEq(c.totalRaised, 7 ether);
        assertEq(c.backerCount, 2);
        assertEq(cf.contributions(id, alice), 3 ether);
        assertEq(address(cf).balance, 7 ether);
    }

    function test_RevertWhen_ContributionIsZero() public {
        uint256 id = _create(_pcts3());
        vm.expectRevert(MilestoneCrowdfund.ZeroAmount.selector);
        _back(id, alice, 0);
    }

    function test_RevertWhen_CreatorContributes() public {
        uint256 id = _create(_pcts3());
        vm.deal(creator, 1 ether);
        vm.expectRevert(MilestoneCrowdfund.CreatorCannotContribute.selector);
        _back(id, creator, 1 ether);
    }

    function test_RevertWhen_ContributingAfterDeadline() public {
        uint256 id = _create(_pcts3());
        vm.warp(block.timestamp + FUNDING);
        vm.expectRevert(MilestoneCrowdfund.FundingClosed.selector);
        _back(id, alice, 1 ether);
    }

    function test_RevertWhen_CampaignDoesNotExist() public {
        vm.expectRevert(abi.encodeWithSelector(MilestoneCrowdfund.CampaignNotFound.selector, 42));
        _back(42, alice, 1 ether);
    }

    function test_RevertWhen_FinalizingBeforeDeadline() public {
        uint256 id = _create(_pcts3());
        vm.expectRevert(MilestoneCrowdfund.FundingStillOpen.selector);
        cf.finalizeFunding(id);
    }

    function test_FinalizeFunding_GoalReachedActivates() public {
        uint256 id = _fundedCampaign();
        assertEq(uint8(_state(id)), uint8(MilestoneCrowdfund.CampaignState.Active));
        vm.expectRevert(MilestoneCrowdfund.InvalidState.selector);
        cf.finalizeFunding(id);
    }

    function test_FinalizeFunding_GoalMissedFailsAndRefundsInFull() public {
        uint256 id = _create(_pcts3());
        _back(id, alice, 4 ether);
        _back(id, bob, 1 ether);
        vm.warp(block.timestamp + FUNDING);
        cf.finalizeFunding(id);
        assertEq(uint8(_state(id)), uint8(MilestoneCrowdfund.CampaignState.Failed));

        uint256 before = alice.balance;
        vm.expectEmit(true, true, false, true);
        emit Refunded(id, alice, 4 ether);
        vm.prank(alice);
        cf.claimRefund(id);
        assertEq(alice.balance - before, 4 ether);
        assertEq(cf.contributions(id, alice), 0);

        vm.prank(alice);
        vm.expectRevert(MilestoneCrowdfund.NothingToRefund.selector);
        cf.claimRefund(id);
    }

    function test_CancelCampaign_OnlyCreatorDuringFunding() public {
        uint256 id = _create(_pcts3());
        _back(id, alice, 1 ether);

        vm.prank(stranger);
        vm.expectRevert(MilestoneCrowdfund.NotCreator.selector);
        cf.cancelCampaign(id);

        vm.prank(creator);
        cf.cancelCampaign(id);
        assertEq(uint8(_state(id)), uint8(MilestoneCrowdfund.CampaignState.Failed));
        assertEq(cf.refundableAmount(id, alice), 1 ether);
    }

    function test_RevertWhen_RefundingActiveCampaign() public {
        uint256 id = _fundedCampaign();
        vm.prank(alice);
        vm.expectRevert(MilestoneCrowdfund.InvalidState.selector);
        cf.claimRefund(id);
    }

    // ------------------------------------------------------------------
    // Milestones and voting
    // ------------------------------------------------------------------

    function test_HappyPath_AllMilestonesReleasedAndCompleted() public {
        uint256 id = _fundedCampaign();

        _approveCurrent(id);
        assertEq(cf.withdrawableAmount(id), 3 ether);
        assertEq(cf.getCampaign(id).currentMilestone, 1);

        uint256 before = creator.balance;
        vm.prank(creator);
        cf.withdraw(id);
        assertEq(creator.balance - before, 3 ether);

        _approveCurrent(id);
        _approveCurrent(id);
        assertEq(uint8(_state(id)), uint8(MilestoneCrowdfund.CampaignState.Completed));

        vm.prank(creator);
        cf.withdraw(id);
        assertEq(creator.balance - before, 10 ether);
        assertEq(address(cf).balance, 0);
    }

    function test_Vote_WeightsByContribution() public {
        uint256 id = _fundedCampaign();
        _submit(id);
        _vote(id, alice, false); // 6 no
        _vote(id, bob, true); // 3 yes
        _vote(id, carol, true); // 1 yes

        MilestoneCrowdfund.Milestone memory m = cf.getMilestone(id, 0);
        assertEq(m.yesWeight, 4 ether);
        assertEq(m.noWeight, 6 ether);

        _closeVote(id);
        assertEq(uint8(cf.getMilestone(id, 0).state), uint8(MilestoneCrowdfund.MilestoneState.Rejected));
        assertEq(cf.withdrawableAmount(id), 0);
    }

    function test_Vote_RejectedWithoutQuorum() public {
        uint256 id = _fundedCampaign();
        _submit(id);
        _vote(id, carol, true); // 1 of 10 ether = 10% turnout < 20% quorum
        _closeVote(id);
        assertEq(uint8(cf.getMilestone(id, 0).state), uint8(MilestoneCrowdfund.MilestoneState.Rejected));
    }

    function test_RevertWhen_VotingTwice() public {
        uint256 id = _fundedCampaign();
        _submit(id);
        _vote(id, alice, true);
        vm.expectRevert(MilestoneCrowdfund.AlreadyVoted.selector);
        _vote(id, alice, false);
    }

    function test_RevertWhen_NonBackerVotes() public {
        uint256 id = _fundedCampaign();
        _submit(id);
        vm.expectRevert(MilestoneCrowdfund.NotBacker.selector);
        _vote(id, stranger, true);
    }

    function test_RevertWhen_VotingAfterDeadline() public {
        uint256 id = _fundedCampaign();
        _submit(id);
        vm.warp(block.timestamp + VOTING);
        vm.expectRevert(MilestoneCrowdfund.VotingClosed.selector);
        _vote(id, alice, true);
    }

    function test_RevertWhen_VotingWithoutSubmission() public {
        uint256 id = _fundedCampaign();
        vm.expectRevert(MilestoneCrowdfund.InvalidMilestoneState.selector);
        _vote(id, alice, true);
    }

    function test_RevertWhen_FinalizingOpenVote() public {
        uint256 id = _fundedCampaign();
        _submit(id);
        vm.expectRevert(MilestoneCrowdfund.VotingStillOpen.selector);
        cf.finalizeMilestone(id);
    }

    function test_RevertWhen_NonCreatorSubmits() public {
        uint256 id = _fundedCampaign();
        vm.prank(alice);
        vm.expectRevert(MilestoneCrowdfund.NotCreator.selector);
        cf.submitMilestone(id, PROOF);
    }

    function test_RevertWhen_SubmittingDuringVote() public {
        uint256 id = _fundedCampaign();
        _submit(id);
        vm.expectRevert(MilestoneCrowdfund.InvalidMilestoneState.selector);
        _submit(id);
    }

    function test_RevertWhen_SubmittingEmptyProof() public {
        uint256 id = _fundedCampaign();
        vm.prank(creator);
        vm.expectRevert(MilestoneCrowdfund.InvalidHash.selector);
        cf.submitMilestone(id, bytes32(0));
    }

    function test_Resubmission_StartsFreshVoteRound() public {
        uint256 id = _fundedCampaign();
        _rejectCurrent(id);

        // Second attempt: alice may vote again because rounds are tracked per attempt.
        _submit(id);
        MilestoneCrowdfund.Milestone memory m = cf.getMilestone(id, 0);
        assertEq(m.attempts, 2);
        assertEq(m.yesWeight + m.noWeight, 0);
        assertTrue(cf.hasVoted(id, 0, 1, alice));
        assertFalse(cf.hasVoted(id, 0, 2, alice));

        _vote(id, alice, true);
        _closeVote(id);
        assertEq(uint8(cf.getMilestone(id, 0).state), uint8(MilestoneCrowdfund.MilestoneState.Approved));
        assertEq(cf.withdrawableAmount(id), 3 ether);
    }

    function test_MaxAttempts_FailsCampaignAndRefundsProRata() public {
        uint256 id = _fundedCampaign();
        _approveCurrent(id); // 3 ether released

        _rejectCurrent(id);
        _rejectCurrent(id);
        assertEq(uint8(_state(id)), uint8(MilestoneCrowdfund.CampaignState.Active));
        _rejectCurrent(id);
        assertEq(uint8(_state(id)), uint8(MilestoneCrowdfund.CampaignState.Failed));

        // 7 ether remains unreleased; alice owns 60% of it.
        assertEq(cf.refundableAmount(id, alice), 4.2 ether);
        assertEq(cf.refundableAmount(id, bob), 2.1 ether);
        assertEq(cf.refundableAmount(id, carol), 0.7 ether);

        vm.prank(alice);
        cf.claimRefund(id);
        vm.prank(bob);
        cf.claimRefund(id);
        vm.prank(carol);
        cf.claimRefund(id);

        // The creator keeps the milestone that was approved before the failure.
        vm.prank(creator);
        cf.withdraw(id);
        assertEq(address(cf).balance, 0);
    }

    function test_FailInactiveCampaign_AfterWindow() public {
        uint256 id = _fundedCampaign();

        vm.expectRevert(MilestoneCrowdfund.CreatorStillActive.selector);
        cf.failInactiveCampaign(id);

        vm.warp(block.timestamp + WINDOW + 1);
        vm.prank(creator);
        vm.expectRevert(MilestoneCrowdfund.SubmissionWindowClosed.selector);
        cf.submitMilestone(id, PROOF);

        vm.prank(stranger);
        cf.failInactiveCampaign(id);
        assertEq(uint8(_state(id)), uint8(MilestoneCrowdfund.CampaignState.Failed));
        assertEq(cf.refundableAmount(id, alice), 6 ether);
    }

    function test_RevertWhen_FailingInactiveDuringVote() public {
        uint256 id = _fundedCampaign();
        vm.warp(block.timestamp + WINDOW);
        _submit(id); // submitted at the very end of the window
        vm.warp(block.timestamp + 1);
        vm.expectRevert(MilestoneCrowdfund.InvalidMilestoneState.selector);
        cf.failInactiveCampaign(id);
    }

    // ------------------------------------------------------------------
    // Payout access control and reentrancy
    // ------------------------------------------------------------------

    function test_RevertWhen_NonCreatorWithdraws() public {
        uint256 id = _fundedCampaign();
        _approveCurrent(id);
        vm.prank(alice);
        vm.expectRevert(MilestoneCrowdfund.NotCreator.selector);
        cf.withdraw(id);
    }

    function test_RevertWhen_NothingToWithdraw() public {
        uint256 id = _fundedCampaign();
        vm.prank(creator);
        vm.expectRevert(MilestoneCrowdfund.NothingToWithdraw.selector);
        cf.withdraw(id);
    }

    function test_RevertWhen_DirectEtherTransfer() public {
        vm.prank(alice);
        (bool ok,) = address(cf).call{value: 1 ether}("");
        assertFalse(ok);
    }

    function test_Reentrancy_RefundCannotBeDrained() public {
        uint256 id = _create(_pcts3());
        ReentrantBacker attacker = new ReentrantBacker(cf);
        attacker.back{value: 2 ether}(id);
        _back(id, alice, 3 ether);
        vm.warp(block.timestamp + FUNDING);
        cf.finalizeFunding(id); // goal missed -> Failed

        // The nested claimRefund hits the guard, so the outer transfer fails and everything reverts.
        vm.expectRevert(MilestoneCrowdfund.TransferFailed.selector);
        attacker.attack();
        assertEq(address(cf).balance, 5 ether);
        assertEq(cf.contributions(id, address(attacker)), 2 ether);
    }

    // ------------------------------------------------------------------
    // Fuzz tests
    // ------------------------------------------------------------------

    /// @notice Whatever the contributions and however many milestones were approved before
    ///         failure, refunds plus creator payouts never exceed what was raised, and at most
    ///         one wei per backer is lost to rounding.
    function testFuzz_RefundsNeverExceedUnreleasedFunds(uint96 a, uint96 b, uint96 c, uint8 approvedCount) public {
        a = uint96(bound(a, 1, 50 ether));
        b = uint96(bound(b, 1, 50 ether));
        c = uint96(bound(c, 1, 50 ether));
        approvedCount = uint8(bound(approvedCount, 0, 2));

        vm.prank(creator);
        uint256 id = cf.createCampaign(1, FUNDING, VOTING, WINDOW, _pcts3(), META);
        _back(id, alice, a);
        _back(id, bob, b);
        _back(id, carol, c);
        vm.warp(block.timestamp + FUNDING);
        cf.finalizeFunding(id);

        for (uint256 i; i < approvedCount; ++i) {
            _submit(id);
            _vote(id, alice, true);
            _vote(id, bob, true);
            _vote(id, carol, true);
            _closeVote(id);
        }
        vm.warp(block.timestamp + WINDOW + 1);
        cf.failInactiveCampaign(id);

        uint256 raised = uint256(a) + b + c;
        uint256 paid;
        uint256 balBefore;
        address[3] memory backers = [alice, bob, carol];
        for (uint256 i; i < 3; ++i) {
            // A dust contribution can round down to a zero refund, which reverts by design.
            if (cf.refundableAmount(id, backers[i]) == 0) continue;
            balBefore = backers[i].balance;
            vm.prank(backers[i]);
            cf.claimRefund(id);
            paid += backers[i].balance - balBefore;
        }
        uint256 released = cf.withdrawableAmount(id);
        if (released > 0) {
            vm.prank(creator);
            cf.withdraw(id);
        }

        assertLe(paid + released, raised);
        assertLe(raised - (paid + released), 3);
        assertEq(address(cf).balance, raised - paid - released);
    }

    /// @notice Releases across all milestones always sum to exactly totalRaised.
    function testFuzz_FullReleaseSumsToTotalRaised(uint96 amount, uint16 p0, uint16 p1) public {
        amount = uint96(bound(amount, 1, 1_000 ether));
        p0 = uint16(bound(p0, 1, 9_998));
        p1 = uint16(bound(p1, 1, 9_999 - p0));
        uint16[] memory p = new uint16[](3);
        p[0] = p0;
        p[1] = p1;
        p[2] = 10_000 - p0 - p1;

        vm.prank(creator);
        uint256 id = cf.createCampaign(1, FUNDING, VOTING, WINDOW, p, META);
        vm.deal(alice, amount);
        _back(id, alice, amount);
        vm.warp(block.timestamp + FUNDING);
        cf.finalizeFunding(id);
        for (uint256 i; i < 3; ++i) {
            _approveCurrent(id);
        }

        assertEq(cf.withdrawableAmount(id), amount);
        assertEq(uint8(_state(id)), uint8(MilestoneCrowdfund.CampaignState.Completed));
    }
}
