package oss.roost.mobile.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import oss.roost.mobile.AppContainer
import oss.roost.mobile.ui.dashboard.DashboardScreen
import oss.roost.mobile.ui.pair.PairScreen
import oss.roost.mobile.ui.session.SessionScreen

/** Route names. Session takes a job id arg. */
object Routes {
    const val PAIR = "pair"
    const val DASHBOARD = "dashboard"
    const val SESSION = "session"
    fun session(jobId: String) = "session/$jobId"
}

/**
 * Top-level navigation. The start destination follows paired state; a later 401 flips
 * `isPaired` false and we pop back to the pairing screen (API.md §1).
 */
@Composable
fun RoostNavHost(
    container: AppContainer,
    isPaired: Boolean,
    pendingDeepLink: String?,
    onDeepLinkConsumed: () -> Unit,
) {
    val nav = rememberNavController()
    val start = if (isPaired) Routes.DASHBOARD else Routes.PAIR

    // When pairing is dropped (401), make sure we are on the pairing screen.
    LaunchedEffect(isPaired) {
        if (!isPaired) {
            nav.navigate(Routes.PAIR) {
                popUpTo(0) { inclusive = true }
            }
        }
    }

    NavHost(navController = nav, startDestination = start) {
        composable(Routes.PAIR) {
            PairScreen(
                container = container,
                deepLink = pendingDeepLink,
                onDeepLinkConsumed = onDeepLinkConsumed,
                onPaired = {
                    nav.navigate(Routes.DASHBOARD) {
                        popUpTo(Routes.PAIR) { inclusive = true }
                    }
                },
            )
        }
        composable(Routes.DASHBOARD) {
            DashboardScreen(
                container = container,
                onOpenSession = { id -> nav.navigate(Routes.session(id)) },
            )
        }
        composable(
            route = "${Routes.SESSION}/{jobId}",
            arguments = listOf(navArgument("jobId") { type = NavType.StringType }),
        ) { entry ->
            val jobId = entry.arguments?.getString("jobId").orEmpty()
            SessionScreen(
                container = container,
                jobId = jobId,
                onOpenChild = { childId -> nav.navigate(Routes.session(childId)) },
                onBack = { nav.popBackStack() },
            )
        }
    }
}
