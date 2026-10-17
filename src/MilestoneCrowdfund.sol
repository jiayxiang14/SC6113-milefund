// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title MilestoneCrowdfund
/// @notice Milestone-based crowdfunding with backer governance.
/// @dev Funds raised by a campaign are locked in this contract and released to the
///      creator one milestone at a time. Each release must be approved by backers in a
///      contribution-weighted vote. If a campaign fails (goal missed, cancelled, creator
///      inactive, or a milestone rejected too many times), backers reclaim the unreleased
///      funds pro rata. All payouts use the pull pattern.
contract MilestoneCrowdfund is ReentrancyGuard {
    // ------------------------------------------------------------------
    // Types
    // ------------------------------------------------------------------

    enum CampaignState {
        Funding,
        Active,
        Completed,
        Failed
    }

    enum MilestoneState {
        Pending,
        Voting,
        Approved,
        Rejected
    }

    /// @dev Field order is chosen so the struct packs into 5 storage slots.
    struct Campaign {
        address creator; //            slot 0 (20 bytes)
        uint64 deadline; //            slot 0 (8)  end of the funding period
        uint8 milestoneCount; //       slot 0 (1)
        uint8 currentMilestone; //     slot 0 (1)
        CampaignState state; //        slot 0 (1)
        uint128 goal; //               slot 1
        uint128 totalRaised; //        slot 1
        uint128 totalReleased; //      slot 2  amount approved for the creator
        uint128 totalWithdrawn; //     slot 2  amount the creator has already pulled
        uint32 votingDuration; //      slot 3
        uint32 milestoneWindow; //     slot 3  time the creator has to submit each milestone
        uint64 phaseStart; //          slot 3  start of the current submission window
        uint32 backerCount; //         slot 3
        bytes32 metadataHash; //       slot 4  keccak256 of the off-chain campaign description
    }

    struct Milestone {
        uint16 percentageBps; //       share of totalRaised released on approval
        MilestoneState state;
        uint8 attempts; //             number of times the creator has submitted this milestone
        uint64 voteDeadline;
        uint128 yesWeight;
        uint128 noWeight;
        bytes32 proofHash; //          keccak256 of the off-chain proof of completion
    }

    // ------------------------------------------------------------------
    // Constants
    // ------------------------------------------------------------------

    uint16 public constant BPS = 10_000;
    /// @notice Minimum turnout (share of totalRaised that must vote) for a milestone to pass.
    uint16 public constant QUORUM_BPS = 2_000;
    uint8 public constant MAX_MILESTONES = 5;
    /// @notice Submissions allowed per milestone (the first one plus two resubmissions).
    uint8 public constant MAX_ATTEMPTS = 3;
    /// @dev Short minimums keep the full lifecycle demonstrable on a testnet.
    uint32 public constant MIN_DURATION = 2 minutes;
    uint32 public constant MAX_FUNDING_DURATION = 90 days;
    uint32 public constant MAX_VOTING_DURATION = 14 days;
    uint32 public constant MAX_MILESTONE_WINDOW = 180 days;

    // ------------------------------------------------------------------
    // Storage
    // ------------------------------------------------------------------

    uint256 public campaignCount;

    mapping(uint256 => Campaign) private _campaigns;
    mapping(uint256 => mapping(uint256 => Milestone)) private _milestones;
    /// @notice campaignId => backer => contributed wei (zeroed after a refund is claimed)
    mapping(uint256 => mapping(address => uint256)) public contributions;
    /// @dev campaignId => milestoneIndex => attempt => voter => voted
    mapping(uint256 => mapping(uint256 => mapping(uint256 => mapping(address => bool)))) private _hasVoted;

    // ------------------------------------------------------------------
    // Events
    // ------------------------------------------------------------------

    event CampaignCreated(
        uint256 indexed campaignId,
        address indexed creator,
        uint256 goal,
        uint64 deadline,
        uint8 milestoneCount,
        bytes32 metadataHash
    );
    event Contributed(uint256 indexed campaignId, address indexed backer, uint256 amount, uint256 totalRaised);
    event CampaignStateChanged(uint256 indexed campaignId, CampaignState state);
    event MilestoneSubmitted(
        uint256 indexed campaignId, uint8 indexed milestoneIndex, uint8 attempt, bytes32 proofHash, uint64 voteDeadline
    );
    event Voted(
        uint256 indexed campaignId,
        uint8 indexed milestoneIndex,
        uint8 attempt,
        address indexed voter,
        bool approve,
        uint256 weight
    );
    event MilestoneFinalized(
        uint256 indexed campaignId,
        uint8 indexed milestoneIndex,
        uint8 attempt,
        bool approved,
        uint256 yesWeight,
        uint256 noWeight,
        uint256 releasedAmount
    );
    event FundsWithdrawn(uint256 indexed campaignId, address indexed creator, uint256 amount);
    event Refunded(uint256 indexed campaignId, address indexed backer, uint256 amount);

    // ------------------------------------------------------------------
    // Errors
    // ------------------------------------------------------------------

    error CampaignNotFound(uint256 campaignId);
    error InvalidGoal();
    error InvalidDuration();
    error InvalidMilestones();
    error InvalidPercentages();
    error InvalidHash();
    error InvalidState();
    error InvalidMilestoneState();
    error NotCreator();
    error CreatorCannotContribute();
    error ZeroAmount();
    error AmountTooLarge();
    error FundingClosed();
    error FundingStillOpen();
    error SubmissionWindowClosed();
    error CreatorStillActive();
    error NotBacker();
    error AlreadyVoted();
    error VotingClosed();
    error VotingStillOpen();
    error NothingToWithdraw();
    error NothingToRefund();
    error TransferFailed();

    // ------------------------------------------------------------------
    // Modifiers
    // ------------------------------------------------------------------

    modifier campaignExists(uint256 campaignId) {
        if (campaignId >= campaignCount) revert CampaignNotFound(campaignId);
        _;
    }

    modifier onlyCreator(uint256 campaignId) {
        if (msg.sender != _campaigns[campaignId].creator) revert NotCreator();
        _;
    }

    // ------------------------------------------------------------------
    // Campaign lifecycle
    // ------------------------------------------------------------------

    /// @notice Create a campaign with 1-5 milestones whose shares sum to 100%.
    /// @param goal Funding target in wei.
    /// @param fundingDuration Seconds the campaign accepts contributions.
    /// @param votingDuration Seconds each milestone vote stays open.
    /// @param milestoneWindow Seconds the creator has to submit each milestone.
    /// @param percentagesBps Share of the funds released per milestone, in basis points.
    /// @param metadataHash keccak256 of the off-chain description stored by the backend.
    function createCampaign(
        uint128 goal,
        uint32 fundingDuration,
        uint32 votingDuration,
        uint32 milestoneWindow,
        uint16[] calldata percentagesBps,
        bytes32 metadataHash
    ) external returns (uint256 campaignId) {
        if (goal == 0) revert InvalidGoal();
        if (
            fundingDuration < MIN_DURATION || fundingDuration > MAX_FUNDING_DURATION || votingDuration < MIN_DURATION
                || votingDuration > MAX_VOTING_DURATION || milestoneWindow < MIN_DURATION
                || milestoneWindow > MAX_MILESTONE_WINDOW
        ) revert InvalidDuration();
        uint256 count = percentagesBps.length;
        if (count == 0 || count > MAX_MILESTONES) revert InvalidMilestones();
        if (metadataHash == bytes32(0)) revert InvalidHash();

        campaignId = campaignCount++;
        uint256 sum;
        for (uint256 i; i < count; ++i) {
            uint16 pct = percentagesBps[i];
            if (pct == 0) revert InvalidPercentages();
            sum += pct;
            _milestones[campaignId][i].percentageBps = pct;
        }
        if (sum != BPS) revert InvalidPercentages();

        uint64 deadline = uint64(block.timestamp) + fundingDuration;
        Campaign storage c = _campaigns[campaignId];
        c.creator = msg.sender;
        c.deadline = deadline;
        // casting to 'uint8' is safe because count <= MAX_MILESTONES
        // forge-lint: disable-next-line(unsafe-typecast)
        c.milestoneCount = uint8(count);
        c.goal = goal;
        c.votingDuration = votingDuration;
        c.milestoneWindow = milestoneWindow;
        c.metadataHash = metadataHash;

        // forge-lint: disable-next-line(unsafe-typecast)
        emit CampaignCreated(campaignId, msg.sender, goal, deadline, uint8(count), metadataHash);
    }

    /// @notice Back a campaign while its funding period is open.
    function contribute(uint256 campaignId) external payable campaignExists(campaignId) {
        Campaign storage c = _campaigns[campaignId];
        if (c.state != CampaignState.Funding) revert InvalidState();
        if (block.timestamp >= c.deadline) revert FundingClosed();
        if (msg.sender == c.creator) revert CreatorCannotContribute();
        if (msg.value == 0) revert ZeroAmount();
        uint256 newTotal = uint256(c.totalRaised) + msg.value;
        if (newTotal > type(uint128).max) revert AmountTooLarge();

        if (contributions[campaignId][msg.sender] == 0) c.backerCount++;
        contributions[campaignId][msg.sender] += msg.value;
        // casting to 'uint128' is safe because of the AmountTooLarge check above
        // forge-lint: disable-next-line(unsafe-typecast)
        c.totalRaised = uint128(newTotal);

        emit Contributed(campaignId, msg.sender, msg.value, newTotal);
    }

    /// @notice Close the funding period. Anyone may call once the deadline has passed.
    function finalizeFunding(uint256 campaignId) external campaignExists(campaignId) {
        Campaign storage c = _campaigns[campaignId];
        if (c.state != CampaignState.Funding) revert InvalidState();
        if (block.timestamp < c.deadline) revert FundingStillOpen();

        if (c.totalRaised >= c.goal) {
            c.phaseStart = uint64(block.timestamp);
            _setState(campaignId, c, CampaignState.Active);
        } else {
            _setState(campaignId, c, CampaignState.Failed);
        }
    }

    /// @notice The creator may cancel during the funding period; backers get full refunds.
    function cancelCampaign(uint256 campaignId) external campaignExists(campaignId) onlyCreator(campaignId) {
        Campaign storage c = _campaigns[campaignId];
        if (c.state != CampaignState.Funding) revert InvalidState();
        _setState(campaignId, c, CampaignState.Failed);
    }

    // ------------------------------------------------------------------
    // Milestones and voting
    // ------------------------------------------------------------------

    /// @notice Submit proof that the current milestone is complete and open the vote.
    function submitMilestone(uint256 campaignId, bytes32 proofHash)
        external
        campaignExists(campaignId)
        onlyCreator(campaignId)
    {
        Campaign storage c = _campaigns[campaignId];
        if (c.state != CampaignState.Active) revert InvalidState();
        if (proofHash == bytes32(0)) revert InvalidHash();
        if (block.timestamp > uint256(c.phaseStart) + c.milestoneWindow) revert SubmissionWindowClosed();

        uint8 index = c.currentMilestone;
        Milestone storage m = _milestones[campaignId][index];
        if (m.state != MilestoneState.Pending && m.state != MilestoneState.Rejected) revert InvalidMilestoneState();

        uint64 voteDeadline = uint64(block.timestamp) + c.votingDuration;
        m.attempts++;
        m.state = MilestoneState.Voting;
        m.voteDeadline = voteDeadline;
        m.yesWeight = 0;
        m.noWeight = 0;
        m.proofHash = proofHash;

        emit MilestoneSubmitted(campaignId, index, m.attempts, proofHash, voteDeadline);
    }

    /// @notice Vote on the milestone under review. Voting weight equals the caller's contribution.
    /// @dev Contributions are frozen once funding closes, so weights cannot be inflated mid-vote.
    function vote(uint256 campaignId, bool approve) external campaignExists(campaignId) {
        Campaign storage c = _campaigns[campaignId];
        if (c.state != CampaignState.Active) revert InvalidState();
        uint8 index = c.currentMilestone;
        Milestone storage m = _milestones[campaignId][index];
        if (m.state != MilestoneState.Voting) revert InvalidMilestoneState();
        if (block.timestamp >= m.voteDeadline) revert VotingClosed();
        uint256 weight = contributions[campaignId][msg.sender];
        if (weight == 0) revert NotBacker();
        mapping(address => bool) storage voted = _hasVoted[campaignId][index][m.attempts];
        if (voted[msg.sender]) revert AlreadyVoted();

        voted[msg.sender] = true;
        // weight <= totalRaised <= type(uint128).max, so the casts cannot truncate.
        // forge-lint: disable-next-line(unsafe-typecast)
        if (approve) m.yesWeight += uint128(weight);
        // forge-lint: disable-next-line(unsafe-typecast)
        else m.noWeight += uint128(weight);

        emit Voted(campaignId, index, m.attempts, msg.sender, approve, weight);
    }

    /// @notice Tally the vote once it has closed. Anyone may call.
    /// @dev Passes if yes > no and turnout reaches QUORUM_BPS of totalRaised. Approval credits
    ///      the milestone's share to the creator; the final milestone receives the remainder so
    ///      rounding never leaves funds stranded.
    function finalizeMilestone(uint256 campaignId) external campaignExists(campaignId) {
        Campaign storage c = _campaigns[campaignId];
        if (c.state != CampaignState.Active) revert InvalidState();
        uint8 index = c.currentMilestone;
        Milestone storage m = _milestones[campaignId][index];
        if (m.state != MilestoneState.Voting) revert InvalidMilestoneState();
        if (block.timestamp < m.voteDeadline) revert VotingStillOpen();

        uint256 yes = m.yesWeight;
        uint256 no = m.noWeight;
        bool passed = yes > no && (yes + no) * BPS >= uint256(c.totalRaised) * QUORUM_BPS;

        uint256 released;
        if (passed) {
            m.state = MilestoneState.Approved;
            bool last = index + 1 == c.milestoneCount;
            released = last ? c.totalRaised - c.totalReleased : uint256(c.totalRaised) * m.percentageBps / BPS;
            // casting to 'uint128' is safe because released <= totalRaised
            // forge-lint: disable-next-line(unsafe-typecast)
            c.totalReleased += uint128(released);
            emit MilestoneFinalized(campaignId, index, m.attempts, true, yes, no, released);
            if (last) {
                _setState(campaignId, c, CampaignState.Completed);
            } else {
                c.currentMilestone = index + 1;
                c.phaseStart = uint64(block.timestamp);
            }
        } else {
            m.state = MilestoneState.Rejected;
            emit MilestoneFinalized(campaignId, index, m.attempts, false, yes, no, 0);
            if (m.attempts >= MAX_ATTEMPTS) {
                _setState(campaignId, c, CampaignState.Failed);
            } else {
                // Give the creator a fresh window to address feedback and resubmit.
                c.phaseStart = uint64(block.timestamp);
            }
        }
    }

    /// @notice Fail a campaign whose creator missed the submission window. Anyone may call.
    function failInactiveCampaign(uint256 campaignId) external campaignExists(campaignId) {
        Campaign storage c = _campaigns[campaignId];
        if (c.state != CampaignState.Active) revert InvalidState();
        if (_milestones[campaignId][c.currentMilestone].state == MilestoneState.Voting) revert InvalidMilestoneState();
        if (block.timestamp <= uint256(c.phaseStart) + c.milestoneWindow) revert CreatorStillActive();
        _setState(campaignId, c, CampaignState.Failed);
    }

    // ------------------------------------------------------------------
    // Payouts (pull pattern)
    // ------------------------------------------------------------------

    /// @notice Creator pulls every amount approved so far. Works in any state, so funds
    ///         approved before a later failure still belong to the creator.
    function withdraw(uint256 campaignId) external nonReentrant campaignExists(campaignId) onlyCreator(campaignId) {
        Campaign storage c = _campaigns[campaignId];
        uint256 amount = c.totalReleased - c.totalWithdrawn;
        if (amount == 0) revert NothingToWithdraw();

        c.totalWithdrawn = c.totalReleased;
        _send(msg.sender, amount);

        emit FundsWithdrawn(campaignId, msg.sender, amount);
    }

    /// @notice Backer reclaims their share of the unreleased funds of a failed campaign.
    function claimRefund(uint256 campaignId) external nonReentrant campaignExists(campaignId) {
        Campaign storage c = _campaigns[campaignId];
        if (c.state != CampaignState.Failed) revert InvalidState();
        uint256 amount = _refundable(c, contributions[campaignId][msg.sender]);
        if (amount == 0) revert NothingToRefund();

        contributions[campaignId][msg.sender] = 0;
        _send(msg.sender, amount);

        emit Refunded(campaignId, msg.sender, amount);
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    function getCampaign(uint256 campaignId) external view campaignExists(campaignId) returns (Campaign memory) {
        return _campaigns[campaignId];
    }

    function getMilestone(uint256 campaignId, uint256 index)
        external
        view
        campaignExists(campaignId)
        returns (Milestone memory)
    {
        if (index >= _campaigns[campaignId].milestoneCount) revert InvalidMilestones();
        return _milestones[campaignId][index];
    }

    function getMilestones(uint256 campaignId)
        external
        view
        campaignExists(campaignId)
        returns (Milestone[] memory list)
    {
        uint256 count = _campaigns[campaignId].milestoneCount;
        list = new Milestone[](count);
        for (uint256 i; i < count; ++i) {
            list[i] = _milestones[campaignId][i];
        }
    }

    function hasVoted(uint256 campaignId, uint256 index, uint256 attempt, address voter) external view returns (bool) {
        return _hasVoted[campaignId][index][attempt][voter];
    }

    /// @notice Amount `backer` could claim right now (zero unless the campaign has failed).
    function refundableAmount(uint256 campaignId, address backer)
        external
        view
        campaignExists(campaignId)
        returns (uint256)
    {
        Campaign storage c = _campaigns[campaignId];
        if (c.state != CampaignState.Failed) return 0;
        return _refundable(c, contributions[campaignId][backer]);
    }

    function withdrawableAmount(uint256 campaignId) external view campaignExists(campaignId) returns (uint256) {
        Campaign storage c = _campaigns[campaignId];
        return c.totalReleased - c.totalWithdrawn;
    }

    // ------------------------------------------------------------------
    // Internal
    // ------------------------------------------------------------------

    /// @dev Pro-rata share of the funds never released to the creator. totalRaised is frozen
    ///      after funding closes, so every backer's share is computed against the same base.
    function _refundable(Campaign storage c, uint256 contributed) private view returns (uint256) {
        if (contributed == 0) return 0;
        return contributed * (c.totalRaised - c.totalReleased) / c.totalRaised;
    }

    function _setState(uint256 campaignId, Campaign storage c, CampaignState state) private {
        c.state = state;
        emit CampaignStateChanged(campaignId, state);
    }

    function _send(address to, uint256 amount) private {
        (bool ok,) = to.call{value: amount}("");
        if (!ok) revert TransferFailed();
    }
}
