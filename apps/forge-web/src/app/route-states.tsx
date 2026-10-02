import * as React from "react"
import {useQueryErrorResetBoundary} from "@tanstack/react-query"
import {useRouter, type ErrorComponentProps} from "@tanstack/react-router"

import {WorkspaceView} from "@/components/forge/activity"
import {PageEmpty} from "@/components/forge/empty-state"
import {ErrorCallout} from "@/components/forge/feedback"
import {useShellPage} from "@/components/forge/shell/index"
import {ScopeNav} from "@/app/scope-nav"
import {Button} from "@/components/ui/button"
import {toApiError} from "@/lib/api/index"

/**
 * A functional component that renders an error view for a route when something goes wrong.
 * The sub nav stays the one of the scope you're in (`ScopeNav`), never another scope's.
 *
 * @param {ErrorComponentProps} props - The props passed to the RouteError component.
 * @param {unknown} props.error - Whatever the route threw; `toApiError` turns it into a readable message.
 * @return {JSX.Element} A React element representing the error view with retry functionality.
 */
export function RouteError({error}: ErrorComponentProps) {
    const router = useRouter()
    const queryErrorResetBoundary = useQueryErrorResetBoundary()
    useShellPage({header: {title: "Something went wrong", icon: "warning"}})

    // Lets suspense queries that failed fetch again when the route retries.
    React.useEffect(() => {
        queryErrorResetBoundary.reset()
    }, [queryErrorResetBoundary])

    return (
        <WorkspaceView>
            <ScopeNav/>
            <ErrorCallout
                title="This page couldn't load"
                action={
                    <Button
                        variant="outline"
                        size="sm"
                        onClick={() => void router.invalidate()}
                    >
                        Retry
                    </Button>
                }
            >
                {toApiError(error).message}
            </ErrorCallout>
        </WorkspaceView>
    )
}

/** No route matches the URL, or a loader threw `notFound()`. Keeps the scope's sub nav. */
export function RouteNotFound() {
    const router = useRouter()
    useShellPage({header: {title: "Page not found", icon: "search"}})

    return (
        <>
            <ScopeNav/>
            <PageEmpty
                illustration="search"
                title="Page not found"
                description="The page you're looking for doesn't exist or has moved."
            >
                <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void router.navigate({to: "/"})}
                >
                    Go home
                </Button>
            </PageEmpty>
        </>
    )
}

/** The shell itself failed, so this renders without it. */
export function AppError({error}: ErrorComponentProps) {
    return (
        <PageEmpty
            illustration="error"
            title="Forge couldn't start"
            description={toApiError(error).message}
            className="min-h-dvh"
        >
            <Button
                variant="outline"
                size="sm"
                onClick={() => window.location.reload()}
            >
                Reload
            </Button>
        </PageEmpty>
    )
}
