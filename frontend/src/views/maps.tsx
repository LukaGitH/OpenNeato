import { useCallback, useEffect, useMemo, useState } from "preact/hooks";
import { api } from "../api";
import backSvg from "../assets/icons/back.svg?raw";
import { ErrorBannerStack, useErrorStack } from "../components/error-banner";
import { Icon } from "../components/icon";
import { useNavigate, usePath } from "../components/router";
import type { HistoryFileInfo, MapData } from "../types";
import { normalizeError } from "../utils";
import { HistoryItemView } from "./history/item";
import { HistoryListView } from "./history/list";

export function SavedMapsView() {
    const navigate = useNavigate();
    const path = usePath();
    const [errors, errorStack] = useErrorStack();
    const [files, setFiles] = useState<HistoryFileInfo[]>([]);
    const [loading, setLoading] = useState(true);
    const [selectedMap, setSelectedMap] = useState<MapData | null>(null);
    const [mapEmpty, setMapEmpty] = useState(false);
    const [deleting, setDeleting] = useState(false);

    const selectedName = path.startsWith("/maps/") ? decodeURIComponent(path.slice(6)) : null;
    const selectedFile = useMemo(
        () => (selectedName ? (files.find((f) => f.name === selectedName) ?? null) : null),
        [selectedName, files],
    );

    const sortByDateDesc = (list: HistoryFileInfo[]) =>
        list.sort((a, b) => (b.session?.time ?? 0) - (a.session?.time ?? 0));

    const refresh = useCallback(() => {
        setLoading(true);
        api.getSavedMaps()
            .then((fileList) => setFiles(sortByDateDesc(fileList)))
            .catch((e: unknown) => errorStack.push(normalizeError(e, "Failed to load saved maps")))
            .finally(() => setLoading(false));
    }, [errorStack]);

    useEffect(() => {
        refresh();
    }, [refresh]);

    useEffect(() => {
        if (!selectedName) {
            setSelectedMap(null);
            setMapEmpty(false);
            return;
        }

        setSelectedMap(null);
        setMapEmpty(false);
        api.getSavedMap(selectedName)
            .then((maps) => {
                if (maps.length > 0) {
                    setSelectedMap(maps[0]);
                } else {
                    setMapEmpty(true);
                }
            })
            .catch((e: unknown) => errorStack.push(normalizeError(e, "Failed to load saved map")));
    }, [selectedName, errorStack]);

    const handleBack = useCallback(() => {
        if (selectedName) {
            navigate("/maps");
            errorStack.clear();
        } else {
            navigate("/");
        }
    }, [selectedName, navigate, errorStack]);

    const handleSelect = useCallback(
        (idx: number) => {
            const file = files[idx];
            if (file) navigate(`/maps/${file.name}`);
        },
        [files, navigate],
    );

    const handleDeleteSession = useCallback(
        (idx: number) => {
            const file = files[idx];
            if (!file) return;
            setDeleting(true);
            api.deleteSavedMap(file.name)
                .then(() => api.getSavedMaps())
                .then((fileList) => {
                    setFiles(sortByDateDesc(fileList));
                    if (selectedName === file.name) navigate("/maps");
                })
                .catch((e: unknown) => errorStack.push(normalizeError(e, "Failed to delete saved map")))
                .finally(() => setDeleting(false));
        },
        [files, selectedName, navigate, errorStack],
    );

    const showDetail = selectedName !== null && selectedFile !== null;

    return (
        <>
            <div class="header">
                <button type="button" class="header-back-btn" onClick={handleBack} aria-label="Back">
                    <Icon svg={backSvg} />
                </button>
                <h1>{showDetail ? "Saved Map" : "Saved Maps"}</h1>
                <div class="header-right-spacer" />
            </div>

            <ErrorBannerStack errors={errors} />

            <div class="history-page">
                {loading && <div class="history-empty">Loading...</div>}
                {!loading && !showDetail && (
                    <HistoryListView
                        files={files}
                        hasRecording={false}
                        deleting={deleting}
                        onSelect={handleSelect}
                        onDeleteSession={handleDeleteSession}
                        onDeleteAll={() => undefined}
                        onImported={refresh}
                        onError={errorStack.push}
                        showListActions={false}
                        emptyLabel="No saved maps yet"
                    />
                )}
                {!loading && showDetail && (
                    <HistoryItemView file={selectedFile} map={selectedMap} mapEmpty={mapEmpty} recording={false} />
                )}
            </div>
        </>
    );
}
