//! 未分类（0009）的同名提及要归到同一个实体，而不是每提一次新建一个。
//!
//! 空本体下 type_id 一律 NULL；召回 SQL 从前写 `e.type_id = $2`，`= NULL` 永远不为真，
//! 于是一份病历抽出四个「the patient」。现在用 IS NOT DISTINCT FROM。

use sqlx::PgPool;
use uuid::Uuid;

struct Fixture {
    org: Uuid,
    kb: Uuid,
    patient: Uuid,
}

async fn seed(pool: &PgPool) -> anyhow::Result<Fixture> {
    let (org, ws, kb, patient) = (Uuid::now_v7(), Uuid::now_v7(), Uuid::now_v7(), Uuid::now_v7());
    sqlx::query("INSERT INTO organizations (id, name) VALUES ($1, 'untyped-namesake-test')")
        .bind(org)
        .execute(pool)
        .await?;
    sqlx::query("INSERT INTO workspaces (id, org_id, name) VALUES ($1, $2, 'untyped-namesake-test')")
        .bind(ws)
        .bind(org)
        .execute(pool)
        .await?;
    sqlx::query(
        "INSERT INTO knowledge_bases (id, workspace_id, name) VALUES ($1, $2, 'untyped-namesake-test')",
    )
    .bind(kb)
    .bind(ws)
    .execute(pool)
    .await?;
    // 没有类型、没有画像：库里就是这么一个「the patient」
    sqlx::query("INSERT INTO entities (id, kb_id, canonical_name) VALUES ($1, $2, 'the patient')")
        .bind(patient)
        .bind(kb)
        .execute(pool)
        .await?;
    Ok(Fixture { org, kb, patient })
}

async fn teardown(pool: &PgPool, f: &Fixture) -> anyhow::Result<()> {
    sqlx::query("DELETE FROM knowledge_bases WHERE id = $1")
        .bind(f.kb)
        .execute(pool)
        .await?;
    sqlx::query("DELETE FROM organizations WHERE id = $1")
        .bind(f.org)
        .execute(pool)
        .await?;
    Ok(())
}

#[tokio::test]
async fn an_untyped_mention_lands_on_its_untyped_namesake() -> anyhow::Result<()> {
    let Some(url) = utopia_store::test_db::url() else {
        return Ok(());
    };
    let pool = PgPool::connect(&url).await?;
    let f = seed(&pool).await?;
    let run = async {
        let r = utopia_store::resolution::resolve_mention(
            &pool,
            f.kb,
            None,
            "The Patient",
            None,
            None,
            &[],
        )
        .await?;
        assert!(!r.created, "同名未分类实体已经在库里，不该新建");
        assert_eq!(r.entity_id, f.patient, "归到那一个");
        let live: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM entities WHERE kb_id = $1 AND merged_into IS NULL",
        )
        .bind(f.kb)
        .fetch_one(&pool)
        .await?;
        assert_eq!(live, 1, "库里仍只有一个实体");
        anyhow::Ok(())
    }
    .await;
    teardown(&pool, &f).await?;
    run
}
