import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api } from "../api";
import backSvg from "../assets/icons/back.svg?raw";
import boltSvg from "../assets/icons/bolt.svg?raw";
import sparkleSvg from "../assets/icons/sparkle.svg?raw";
import stopSvg from "../assets/icons/stop.svg?raw";
import { BatteryIcon } from "../components/battery-icon";
import { Icon } from "../components/icon";
import type { JoystickValue } from "../components/joystick";
import { Joystick } from "../components/joystick";
import { LidarMap } from "../components/lidar-map";
import { useNavigate } from "../components/router";
import { usePolling } from "../hooks/use-polling";
import type { ChargerData, LidarScan, ManualStatus, SettingsData } from "../types";

// Convert joystick X/Y to differential wheel distances (mm).
// We send a large fixed distance so the robot keeps moving continuously until
// the next command changes direction or a stop cancels it. The firmware watchdog
// stops wheels if no command arrives within MANUAL_MOVE_TIMEOUT_MS (2s).
const MOVE_DIST_MM = 10000; // Protocol max — robot moves until next command
const DEFAULT_MAX_SPEED_MM_S = 120;
const DEFAULT_TURN_SCALE = 0.8;
const MOVE_SEND_THROTTLE_MS = 180;
const MOVE_KEEPALIVE_MS = 1500;
const DRIVE_RAMP_INTERVAL_MS = 50;
const DRIVE_RAMP_UNITS_PER_SEC = 0.8;

interface WheelCommand {
    left: number;
    right: number;
    speed: number;
}

interface DriveAxes {
    speed: number;
    turn: number;
}

function clamp(val: number, min: number, max: number): number {
    return val < min ? min : val > max ? max : val;
}

function curveAxis(val: number): number {
    return Math.sign(val) * val * val;
}

function axesToWheels(axes: DriveAxes, maxSpeed: number, turnScale: number): WheelCommand {
    if (axes.speed === 0 && axes.turn === 0) return { left: 0, right: 0, speed: 0 };

    const fwdAxis = curveAxis(axes.speed);
    const turnAxis = curveAxis(axes.turn) * turnScale;
    const speedFactor = clamp(Math.max(Math.abs(fwdAxis), Math.abs(turnAxis)), 0, 1);
    const speed = Math.max(1, Math.round(speedFactor * maxSpeed));
    // Normalize direction from joystick, then apply fixed long distance.
    // The ratio of left/right determines the turn arc; speed controls pace.
    const fwd = fwdAxis * MOVE_DIST_MM;
    const turn = turnAxis * MOVE_DIST_MM;

    // Robot rejects LWheelDist/RWheelDist outside ±10000mm
    const left = Math.round(clamp(fwd + turn, -MOVE_DIST_MM, MOVE_DIST_MM));
    const right = Math.round(clamp(fwd - turn, -MOVE_DIST_MM, MOVE_DIST_MM));
    return { left, right, speed };
}

function isStopCommand(cmd: WheelCommand): boolean {
    return cmd.left === 0 && cmd.right === 0;
}

function commandChanged(a: WheelCommand | null, b: WheelCommand): boolean {
    if (!a) return true;
    return Math.abs(a.left - b.left) >= 500 || Math.abs(a.right - b.right) >= 500 || Math.abs(a.speed - b.speed) >= 10;
}

interface ManualViewProps {
    isManual: boolean;
    status: ManualStatus | null;
    brush: boolean;
    vacuum: boolean;
    sideBrush: boolean;
    onToggleBrush: () => Promise<void>;
    onToggleVacuum: () => Promise<void>;
    onToggleSideBrush: () => Promise<void>;
    onToggleAll: () => Promise<void>;
}

function safetyWarning(status: ManualStatus | null): string | null {
    if (!status) return null;
    if (status.lifted) return "Robot is lifted";
    // Stall: direction-aware message
    if (status.stallFront) return "Stall detected — reverse to clear";
    if (status.stallRear) return "Stall detected — move forward to clear";
    // Physical bumper contact
    const bumpers: string[] = [];
    if (status.bumperFrontLeft) bumpers.push("front-left");
    if (status.bumperFrontRight) bumpers.push("front-right");
    if (status.bumperSideLeft) bumpers.push("side-left");
    if (status.bumperSideRight) bumpers.push("side-right");
    if (bumpers.length > 0) return `Bumper: ${bumpers.join(", ")} — reverse to clear`;
    return null;
}

export function ManualView({ isManual, status, brush, vacuum, sideBrush, onToggleAll }: ManualViewProps) {
    const navigate = useNavigate();
    const charger = usePolling<ChargerData>(api.getCharger, 5000);
    const settings = usePolling<SettingsData>(api.getSettings, isManual ? 30000 : 0);
    const [stopping, setStopping] = useState(false);
    const [motorPending, setMotorPending] = useState(false);
    const [moving, setMoving] = useState(false);
    const [mapSize, setMapSize] = useState(280);
    const mapContainerRef = useRef<HTMLDivElement>(null);
    const targetAxes = useRef<DriveAxes>({ speed: 0, turn: 0 });
    const currentAxes = useRef<DriveAxes>({ speed: 0, turn: 0 });
    const rampTimer = useRef<ReturnType<typeof setInterval> | null>(null);
    const lastRampAt = useRef(0);
    const busy = stopping;
    const maxSpeed = settings.data?.manualSpeed ?? DEFAULT_MAX_SPEED_MM_S;
    const turnScale = (settings.data?.manualTurn ?? Math.round(DEFAULT_TURN_SCALE * 100)) / 100;

    const wrapMotorToggle = useCallback(
        (toggle: () => Promise<void>) => {
            if (motorPending) return;
            setMotorPending(true);
            toggle()
                .catch(() => {})
                .finally(() => setMotorPending(false));
        },
        [motorPending],
    );

    // Poll LIDAR only when in manual mode
    const lidar = usePolling<LidarScan>(api.getLidar, isManual ? 1000 : 0);

    // Measure available map container — use the smaller of width/height so the
    // square canvas fits without pushing controls off screen.
    useEffect(() => {
        const el = mapContainerRef.current;
        if (!el) return;
        const observer = new ResizeObserver((entries) => {
            for (const entry of entries) {
                const w = Math.floor(entry.contentRect.width);
                const h = Math.floor(entry.contentRect.height);
                setMapSize(Math.min(w, h));
            }
        });
        observer.observe(el);
        return () => observer.disconnect();
    }, []);

    // Send move command (fire-and-forget). Changed commands are throttled, and
    // the active command is resent as a keepalive while the joystick is held.
    const moveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const lastMove = useRef<WheelCommand | null>(null);
    const lastSent = useRef<WheelCommand | null>(null);
    const lastSentAt = useRef(0);
    const moveInFlight = useRef(false);
    const queuedMove = useRef<{ cmd: WheelCommand; force: boolean } | null>(null);

    const sendMove = useCallback((cmd: WheelCommand, force = false) => {
        if (!force && !commandChanged(lastSent.current, cmd)) return;
        if (moveInFlight.current) {
            queuedMove.current = { cmd, force };
            return;
        }
        lastSent.current = cmd;
        lastSentAt.current = Date.now();
        moveInFlight.current = true;
        api.manualMove(cmd.left, cmd.right, cmd.speed)
            .catch(() => {})
            .finally(() => {
                moveInFlight.current = false;
                const next = queuedMove.current;
                queuedMove.current = null;
                if (next) sendMove(next.cmd, next.force);
            });
    }, []);

    const queueMove = useCallback(
        (cmd: WheelCommand, force = false) => {
            if (!isManual || busy) return;

            if (isStopCommand(cmd)) {
                if (moveTimer.current) {
                    clearTimeout(moveTimer.current);
                    moveTimer.current = null;
                }
                sendMove(cmd, true);
                return;
            }

            if (force) {
                sendMove(cmd, true);
                return;
            }

            if (moveTimer.current) return;
            const delay = Math.max(0, MOVE_SEND_THROTTLE_MS - (Date.now() - lastSentAt.current));
            moveTimer.current = setTimeout(() => {
                moveTimer.current = null;
                const latest = lastMove.current;
                if (!latest || isStopCommand(latest)) return;
                sendMove(latest);
            }, delay);
        },
        [busy, isManual, sendMove],
    );

    const sendAxes = useCallback(
        (nextAxes: DriveAxes) => {
            const wheels = axesToWheels(nextAxes, maxSpeed, turnScale);
            lastMove.current = wheels;
            setMoving(!isStopCommand(wheels));
            queueMove(wheels);
        },
        [maxSpeed, queueMove, turnScale],
    );

    const updateDrive = useCallback(
        (patch: Partial<DriveAxes>) => {
            if (!isManual || busy) return;
            targetAxes.current = { ...targetAxes.current, ...patch };
        },
        [busy, isManual],
    );

    const onSpeedMove = useCallback(
        (v: JoystickValue) => {
            updateDrive({ speed: v.y });
        },
        [updateDrive],
    );

    const onTurnMove = useCallback(
        (v: JoystickValue) => {
            updateDrive({ turn: v.x });
        },
        [updateDrive],
    );

    const releaseDrive = useCallback(() => {
        targetAxes.current = { speed: 0, turn: 0 };
        currentAxes.current = { speed: 0, turn: 0 };
        lastMove.current = null;
        lastSent.current = null;
        queuedMove.current = null;
        setMoving(false);
        if (moveTimer.current) {
            clearTimeout(moveTimer.current);
            moveTimer.current = null;
        }
        sendMove({ left: 0, right: 0, speed: 0 }, true);
    }, [sendMove]);

    const onSpeedRelease = useCallback(() => {
        if (targetAxes.current.turn === 0) {
            releaseDrive();
        } else {
            updateDrive({ speed: 0 });
        }
    }, [releaseDrive, updateDrive]);

    const onTurnRelease = useCallback(() => {
        if (targetAxes.current.speed === 0) {
            releaseDrive();
        } else {
            updateDrive({ turn: 0 });
        }
    }, [releaseDrive, updateDrive]);

    useEffect(() => {
        if (!isManual || busy) return;
        lastRampAt.current = Date.now();
        rampTimer.current = setInterval(() => {
            const now = Date.now();
            const maxStep = ((now - lastRampAt.current) / 1000) * DRIVE_RAMP_UNITS_PER_SEC;
            lastRampAt.current = now;

            const stepAxis = (cur: number, target: number) => {
                const delta = target - cur;
                if (Math.abs(delta) <= maxStep) return target;
                return cur + Math.sign(delta) * maxStep;
            };

            const next = {
                speed: stepAxis(currentAxes.current.speed, targetAxes.current.speed),
                turn: stepAxis(currentAxes.current.turn, targetAxes.current.turn),
            };
            currentAxes.current = next;
            sendAxes(next);
        }, DRIVE_RAMP_INTERVAL_MS);
        return () => {
            if (rampTimer.current) {
                clearInterval(rampTimer.current);
                rampTimer.current = null;
            }
        };
    }, [busy, isManual, sendAxes]);

    useEffect(() => {
        if (!isManual || busy) return;
        const interval = setInterval(() => {
            const cmd = lastMove.current;
            if (cmd && !isStopCommand(cmd)) queueMove(cmd, true);
        }, MOVE_KEEPALIVE_MS);
        return () => clearInterval(interval);
    }, [busy, isManual, queueMove]);

    useEffect(() => {
        if (isManual && !busy) return;
        targetAxes.current = { speed: 0, turn: 0 };
        currentAxes.current = { speed: 0, turn: 0 };
        lastMove.current = null;
        lastSent.current = null;
        queuedMove.current = null;
        lastSentAt.current = 0;
        setMoving(false);
        if (moveTimer.current) {
            clearTimeout(moveTimer.current);
            moveTimer.current = null;
        }
    }, [busy, isManual]);

    // Navigate away only after polled state confirms manual mode ended
    useEffect(() => {
        if (stopping && !isManual) {
            navigate("/");
        }
    }, [stopping, isManual, navigate]);

    const handleStop = useCallback(() => {
        if (stopping) return;
        releaseDrive();
        setStopping(true);
        api.manual(false).catch(() => setStopping(false));
    }, [releaseDrive, stopping]);

    return (
        <>
            {/* Header */}
            <div class="header">
                <button
                    type="button"
                    class="header-back-btn"
                    aria-label="Back"
                    disabled={stopping}
                    onClick={() => navigate("/")}
                >
                    <Icon svg={backSvg} />
                </button>
                <h1>Manual</h1>
                <div class="header-right-spacer" />
            </div>

            <div class="manual-page">
                {/* LIDAR map — outer div fills remaining height, inner div wraps canvas */}
                <div class="manual-map-sizer" ref={mapContainerRef}>
                    <div class="manual-map">
                        <LidarMap scan={lidar.data} size={mapSize} moving={moving} />
                        {charger.data && (
                            <div class="manual-map-battery">
                                <BatteryIcon pct={charger.data.fuelPercent} />
                                <span>{charger.data.fuelPercent}%</span>
                                {(charger.data.chargingActive || charger.data.extPwrPresent) && <Icon svg={boltSvg} />}
                            </div>
                        )}
                        {!lidar.data && !isManual && (
                            <div class="manual-map-warn manual-map-center">Not in manual mode</div>
                        )}
                        {!lidar.data && isManual && lidar.error && (
                            <div class="manual-map-warn manual-map-center">LIDAR unavailable</div>
                        )}
                        {safetyWarning(status) && (
                            <div class="manual-map-warn manual-map-top error">{safetyWarning(status)}</div>
                        )}
                        {lidar.data &&
                            (lidar.data.validPoints < 90 ||
                                (lidar.data.rotationSpeed > 0 && lidar.data.rotationSpeed < 4.0)) && (
                                <div class="manual-map-warn">
                                    {[
                                        lidar.data.validPoints < 90 && `Low quality (${lidar.data.validPoints}/360)`,
                                        lidar.data.rotationSpeed > 0 &&
                                            lidar.data.rotationSpeed < 4.0 &&
                                            `Slow LDS (${lidar.data.rotationSpeed.toFixed(1)} Hz)`,
                                    ]
                                        .filter(Boolean)
                                        .join(" · ")}
                                </div>
                            )}
                    </div>
                </div>

                {/* Controls area */}
                <div class={`manual-controls${isManual && !busy ? "" : " disabled"}`}>
                    <div class="manual-joystick manual-drive-controls">
                        <div class="manual-axis-control">
                            <Joystick size={112} axis="vertical" onMove={onSpeedMove} onRelease={onSpeedRelease} />
                        </div>
                        <button
                            type="button"
                            class={`manual-motor-btn${brush && vacuum && sideBrush ? " active" : ""}${motorPending ? " pending" : ""}`}
                            disabled={!isManual || busy || motorPending || status?.lifted}
                            onClick={() => wrapMotorToggle(onToggleAll)}
                        >
                            <Icon svg={sparkleSvg} />
                            All
                        </button>
                        <div class="manual-axis-control">
                            <Joystick size={112} axis="horizontal" onMove={onTurnMove} onRelease={onTurnRelease} />
                        </div>
                    </div>
                </div>

                {/* Stop button */}
                <div class="manual-stop">
                    <button
                        type="button"
                        class={`action-btn manual-stop-btn${stopping ? " pending" : ""}`}
                        disabled={!isManual || stopping}
                        onClick={handleStop}
                    >
                        <Icon svg={stopSvg} />
                        Stop
                    </button>
                </div>
            </div>
        </>
    );
}
