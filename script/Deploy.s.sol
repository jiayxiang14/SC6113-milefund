// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {MilestoneCrowdfund} from "../src/MilestoneCrowdfund.sol";

/// @notice Deploys MilestoneCrowdfund and records the address in deployments/<chainId>.json,
///         which the Flask backend reads on start-up.
contract Deploy is Script {
    function run() external returns (MilestoneCrowdfund cf) {
        uint256 startBlock = block.number;
        vm.startBroadcast();
        cf = new MilestoneCrowdfund();
        vm.stopBroadcast();

        string memory obj = "deployment";
        vm.serializeUint(obj, "chainId", block.chainid);
        vm.serializeUint(obj, "startBlock", startBlock);
        string memory json = vm.serializeAddress(obj, "address", address(cf));
        string memory path = string.concat("deployments/", vm.toString(block.chainid), ".json");
        vm.writeJson(json, path);

        console.log("MilestoneCrowdfund deployed at", address(cf));
        console.log("Deployment written to", path);
    }
}
